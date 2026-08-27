"""Unit tests for the MIDI keyboard driver.

Covers the byte parser (running status, interleaved real-time bytes), the
velocity->duration mapping and its per-valve clamps, and the mode gate that
keeps a plugged-in keyboard from firing the rig while the installation is
running its own pipeline.
"""
import importlib
import json
import sys

import pytest


def _reload(monkeypatch, **env):
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    if "bush_midi" in sys.modules:
        del sys.modules["bush_midi"]
    return importlib.import_module("bush_midi")


class FakeClient:
    def __init__(self):
        self.published = []

    def publish(self, topic, payload, retain=False):
        self.published.append((topic, payload))

    def fired(self):
        return [json.loads(p) for t, p in self.published
                if t == "bush/flame/pulse"]


@pytest.fixture
def mod(monkeypatch):
    return _reload(monkeypatch, MIDI_RETRIGGER_MS="0")


def armed_driver(mod):
    d = mod.MidiDriver(FakeClient())
    d.set_mode("midi")
    return d


# ── byte parser ─────────────────────────────────────────────────────────────

def test_parses_a_single_note_on(mod):
    out = list(mod.MidiParser().feed(bytes([0x90, 60, 100])))
    assert out == [(0x90, 60, 100)]


def test_running_status_reuses_the_last_status_byte(mod):
    # A keyboard may omit the repeated status byte between notes.
    out = list(mod.MidiParser().feed(bytes([0x90, 60, 100, 62, 90, 64, 80])))
    assert out == [(0x90, 60, 100), (0x90, 62, 90), (0x90, 64, 80)]


def test_realtime_bytes_interleaved_mid_message_are_ignored(mod):
    # 0xF8 (clock) can arrive between any two bytes of a channel message.
    out = list(mod.MidiParser().feed(bytes([0x90, 0xF8, 60, 0xF8, 100])))
    assert out == [(0x90, 60, 100)]


def test_message_split_across_reads(mod):
    p = mod.MidiParser()
    assert list(p.feed(bytes([0x90, 60]))) == []
    assert list(p.feed(bytes([100]))) == [(0x90, 60, 100)]


def test_data_before_any_status_is_discarded(mod):
    assert list(mod.MidiParser().feed(bytes([60, 100]))) == []


# ── velocity -> duration ────────────────────────────────────────────────────

def test_velocity_scales_between_the_bounds(mod):
    lo = mod.scale_velocity(1, "flare1")
    hi = mod.scale_velocity(127, "flare1")
    assert lo == mod.MIDI_MIN_MS or lo > 0
    assert hi == mod.MIDI_MAX_MS
    assert lo < hi


def test_poof_has_a_tighter_ceiling(mod):
    # A hard keypress must not turn the poofer into a long burn.
    assert mod.scale_velocity(127, "poof1") <= mod.POOF_MAX_MS
    assert mod.scale_velocity(127, "poof1") < mod.scale_velocity(127, "bigjet1")


def test_duration_never_exceeds_the_cap(mod):
    for v in range(0, 128):
        assert mod.scale_velocity(v, "bigjet1") <= mod.MIDI_MAX_MS


# ── note -> valve ───────────────────────────────────────────────────────────

def test_seven_consecutive_notes_map_to_the_seven_valves(mod):
    d = armed_driver(mod)
    base = mod.MIDI_BASE_NOTE
    for i in range(7):
        d.note_on(base + i, 100)
    assert [f["valve"] for f in d.client.fired()] == mod.NOTE_VALVES


def test_notes_outside_the_range_are_ignored(mod):
    d = armed_driver(mod)
    d.note_on(mod.MIDI_BASE_NOTE - 1, 100)
    d.note_on(mod.MIDI_BASE_NOTE + 7, 100)
    assert d.client.fired() == []


def test_note_off_does_not_close_the_valve(mod):
    # The firmware's own timer closes it, so a lost note-off cannot strand a
    # solenoid open. Note-off must therefore publish nothing at all.
    d = armed_driver(mod)
    d.handle(0x80, mod.MIDI_BASE_NOTE, 0)
    d.handle(0x90, mod.MIDI_BASE_NOTE, 0)   # note-on velocity 0 == note-off
    assert d.client.fired() == []


# ── mode gate ───────────────────────────────────────────────────────────────

@pytest.mark.parametrize("mode", ["manual", "interactive", None, "", "bogus"])
def test_notes_do_not_fire_unless_mode_is_midi(mod, mode):
    d = mod.MidiDriver(FakeClient())
    d.set_mode(mode)
    d.note_on(mod.MIDI_BASE_NOTE, 127)
    assert d.client.fired() == []


def test_notes_fire_in_midi_mode(mod):
    d = armed_driver(mod)
    d.note_on(mod.MIDI_BASE_NOTE, 127)
    assert len(d.client.fired()) == 1


def test_control_change_drives_lights_in_any_mode(mod):
    # Knobs are cosmetic; they should not need arming.
    d = mod.MidiDriver(FakeClient())
    d.set_mode("manual")
    d.control_change(mod.CC_BRIGHTNESS, 127)
    topics = [t for t, _ in d.client.published]
    assert "bush/lights/brightness" in topics


# ── retrigger guard ─────────────────────────────────────────────────────────

def test_retrigger_guard_drops_a_machine_gun_repeat(monkeypatch):
    m = _reload(monkeypatch, MIDI_RETRIGGER_MS="5000")
    d = m.MidiDriver(FakeClient())
    d.set_mode("midi")
    d.note_on(m.MIDI_BASE_NOTE, 100)
    d.note_on(m.MIDI_BASE_NOTE, 100)
    d.note_on(m.MIDI_BASE_NOTE, 100)
    assert len(d.client.fired()) == 1


def test_retrigger_guard_is_per_valve(monkeypatch):
    m = _reload(monkeypatch, MIDI_RETRIGGER_MS="5000")
    d = m.MidiDriver(FakeClient())
    d.set_mode("midi")
    d.note_on(m.MIDI_BASE_NOTE, 100)
    d.note_on(m.MIDI_BASE_NOTE + 1, 100)
    assert len(d.client.fired()) == 2

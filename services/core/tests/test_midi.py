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


# ── hold ceilings ───────────────────────────────────────────────────────────

def test_hold_ceilings(mod):
    assert mod.hold_ms("bigjet1") == mod.MIDI_HOLD_MS == 5000
    assert mod.hold_ms("flare2") == 5000
    # The poofer is a sharp effect; a leaned-on key must not make it a long burn.
    assert mod.hold_ms("poof1") == mod.POOF_HOLD_MS == 1000
    assert mod.hold_ms("poof1") < mod.hold_ms("bigjet1")


# ── hold semantics ──────────────────────────────────────────────────────────

def test_key_down_opens_for_the_max_hold(mod):
    d = armed_driver(mod)
    d.note_on(mod.MIDI_BASE_NOTE, 100)
    fired = d.client.fired()
    assert len(fired) == 1
    assert fired[0]["ms"] == mod.MIDI_HOLD_MS


def test_key_up_closes_with_ms_zero(mod):
    d = armed_driver(mod)
    d.note_on(mod.MIDI_BASE_NOTE, 100)
    d.note_off(mod.MIDI_BASE_NOTE)
    fired = d.client.fired()
    assert [f["ms"] for f in fired] == [mod.MIDI_HOLD_MS, 0]
    assert fired[1]["valve"] == fired[0]["valve"]


def test_note_on_velocity_zero_is_a_release(mod):
    # Running status often expresses note-off that way.
    d = armed_driver(mod)
    d.handle(0x90, mod.MIDI_BASE_NOTE, 100)
    d.handle(0x90, mod.MIDI_BASE_NOTE, 0)
    assert [f["ms"] for f in d.client.fired()] == [mod.MIDI_HOLD_MS, 0]


def test_release_without_a_matching_press_is_ignored(mod):
    d = armed_driver(mod)
    d.note_off(mod.MIDI_BASE_NOTE)
    assert d.client.fired() == []


def test_leaving_midi_mode_releases_held_valves(mod):
    d = armed_driver(mod)
    d.note_on(mod.MIDI_BASE_NOTE, 100)
    d.note_on(mod.MIDI_BASE_NOTE + 1, 100)
    d.set_mode("manual")
    closes = [f for f in d.client.fired() if f["ms"] == 0]
    assert len(closes) == 2, "switching away mid-note must not strand a valve"


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


def test_a_lost_key_up_still_has_a_firmware_backstop(mod):
    # The open is a bounded pulse, never a latch: if the key-up never arrives
    # the firmware closes the valve at this ms on its own.
    d = armed_driver(mod)
    d.note_on(mod.MIDI_BASE_NOTE, 100)
    assert d.client.fired()[0]["ms"] > 0


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

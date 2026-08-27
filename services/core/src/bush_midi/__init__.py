#!/usr/bin/env python3
"""
MIDI keyboard -> solenoids and lights.

Reads a raw ALSA rawmidi character device directly rather than going through
mido/rtmidi: MIDI is a byte protocol simple enough to parse inline, and this
avoids building a C extension on the board. The device is root:audio 0660, so
the service only needs the audio group.

Notes hold solenoids open: key down opens the valve, key up closes it, so the
length of the note is the length of the flame. Control changes drive the
lights.

Only acts while bush/mode is "midi", so a keyboard left plugged in cannot
fire the rig when the installation is running its own pipeline.

Safety: a key-down opens the valve for its maximum hold, not indefinitely, and
the key-up closes it early. If a key-up is ever lost — a dropped packet, a
yanked cable, the service dying mid-note — the firmware's own timer still
closes the valve at that maximum. Nothing here can hold a solenoid open
forever, which is why the open is expressed as a bounded pulse rather than a
latch.
"""
import json
import os
import select
import time

from bushutil import make_logger, run_mqtt_service

log = make_logger("midi")

# ── device ─────────────────────────────────────────────────────────────────
# Default matches the Alesis VI25 on this rig; `amidi -l` lists alternatives.
MIDI_DEVICE = os.environ.get("MIDI_DEVICE", "/dev/snd/midiC4D0")

# ── topics ─────────────────────────────────────────────────────────────────
TOPIC_FLAME      = "bush/flame/pulse"
TOPIC_MODE       = "bush/mode"
TOPIC_BRIGHTNESS = "bush/lights/brightness"
TOPIC_LAYOUT     = "bush/lights/layout"

# ── note -> valve ──────────────────────────────────────────────────────────
# Seven consecutive semitones from MIDI_BASE_NOTE, in this order. C3 = 48 by
# the convention CircuitPython and most DAWs use; if your keyboard is
# transposed, move MIDI_BASE_NOTE rather than rewiring anything.
MIDI_BASE_NOTE = int(os.environ.get("MIDI_BASE_NOTE", "48"))
NOTE_VALVES = [v for v in os.environ.get(
    "MIDI_VALVES",
    "flare1,flare2,flare3,bigjet1,bigjet2,bigjet3,poof1").split(",") if v.strip()]

# How long a held key may keep a valve open. The key-up normally closes it
# well before this; these are the ceilings that apply when a key-up is lost,
# and the longest a leaned-on key can burn.
MIDI_HOLD_MS = int(os.environ.get("MIDI_HOLD_MS", "5000"))

# The poofer gets a much shorter ceiling: it is a sharp effect, and a leaned-on
# key should not turn it into a five-second burn.
POOF_HOLD_MS = int(os.environ.get("MIDI_POOF_HOLD_MS", "1000"))

# Ignore a repeated note-on inside this window — cheap protection against a
# stuck key or an over-enthusiastic tremolo emptying the propane.
RETRIGGER_MS = int(os.environ.get("MIDI_RETRIGGER_MS", "120"))

# ── control changes -> lights ──────────────────────────────────────────────
# CC number -> (topic, low, high). The VI25's knobs default to CC 20-27.
CC_BRIGHTNESS = int(os.environ.get("MIDI_CC_BRIGHTNESS", "1"))    # mod wheel
CC_LAYOUT     = int(os.environ.get("MIDI_CC_LAYOUT", "20"))       # first knob

# MIDI status bytes
NOTE_OFF, NOTE_ON, CONTROL_CHANGE = 0x80, 0x90, 0xB0


def hold_ms(valve: str) -> int:
    """Maximum time a held key may keep this valve open."""
    return POOF_HOLD_MS if valve.startswith("poof") else MIDI_HOLD_MS


class MidiParser:
    """Incremental MIDI byte parser.

    Handles running status (a stream may omit the status byte when it repeats)
    and discards System Real-Time bytes, which may appear mid-message.
    """

    def __init__(self):
        self._status = None
        self._data = []

    def feed(self, chunk: bytes):
        """Yield (status, d1, d2) for each complete channel message."""
        for b in chunk:
            if b >= 0xF8:            # System Real-Time — may interleave
                continue
            if b >= 0xF0:            # System Common — resets running status
                self._status = None
                self._data = []
                continue
            if b & 0x80:             # status byte
                self._status = b
                self._data = []
                continue
            if self._status is None:  # data before any status: ignore
                continue
            self._data.append(b)
            if len(self._data) == 2:
                yield (self._status, self._data[0], self._data[1])
                self._data = []      # running status: keep self._status


class MidiDriver:
    def __init__(self, client):
        self.client = client
        self.mode = None
        self._last_fire = {}
        self._held = set()

    # ── mode gate ──────────────────────────────────────────────────────────
    def set_mode(self, mode):
        if mode != self.mode:
            log(f"mode: {self.mode} -> {mode}")
            if self.mode == "midi" and mode != "midi":
                # Switching away mid-note must not leave a valve held.
                self.release_all()
        self.mode = mode

    @property
    def armed(self) -> bool:
        return self.mode == "midi"

    # ── handlers ───────────────────────────────────────────────────────────
    def handle(self, status, d1, d2):
        kind = status & 0xF0
        if kind == NOTE_ON and d2 > 0:
            self.note_on(d1, d2)
        elif kind == NOTE_OFF or (kind == NOTE_ON and d2 == 0):
            # A note-on with velocity 0 is a note-off; many keyboards send it
            # that way under running status.
            self.note_off(d1)
        elif kind == CONTROL_CHANGE:
            self.control_change(d1, d2)

    def _valve_for(self, note):
        idx = note - MIDI_BASE_NOTE
        if idx < 0 or idx >= len(NOTE_VALVES):
            return None
        return NOTE_VALVES[idx]

    def note_on(self, note, velocity):
        valve = self._valve_for(note)
        if valve is None:
            return
        now = time.monotonic()
        if (now - self._last_fire.get(valve, 0.0)) * 1000 < RETRIGGER_MS:
            return
        self._last_fire[valve] = now

        if not self.armed:
            log(f"note {note} -> {valve} ignored (mode={self.mode})")
            return
        # Open for the maximum hold. The key-up closes it early; this value is
        # only reached if the key-up is lost, so it doubles as the ceiling on
        # how long a leaned-on key can burn.
        ms = hold_ms(valve)
        self._held.add(valve)
        self.client.publish(TOPIC_FLAME, json.dumps({"valve": valve, "ms": ms}))
        log(f"note {note} down -> {valve} open (max {ms}ms)")

    def note_off(self, note):
        valve = self._valve_for(note)
        if valve is None or valve not in self._held:
            return
        self._held.discard(valve)
        if not self.armed:
            return
        # ms 0 is an explicit close in the relay firmware.
        self.client.publish(TOPIC_FLAME, json.dumps({"valve": valve, "ms": 0}))
        log(f"note {note} up -> {valve} closed")

    def release_all(self):
        """Close everything we are holding — used when disarming."""
        for valve in sorted(self._held):
            self.client.publish(TOPIC_FLAME,
                                json.dumps({"valve": valve, "ms": 0}))
        if self._held:
            log(f"released {len(self._held)} held valve(s)")
        self._held.clear()

    def control_change(self, cc, value):
        # Lights are cosmetic, so they follow the knobs in any mode — turning
        # a knob should never be the thing that lights the rig on fire, but it
        # also should not require arming.
        if cc == CC_BRIGHTNESS:
            self.client.publish(TOPIC_BRIGHTNESS, str(round(value / 127.0, 3)))
        elif cc == CC_LAYOUT:
            self.client.publish(TOPIC_LAYOUT,
                                "spiral" if value >= 64 else "unordered",
                                retain=True)


def main():
    driver = {}

    def on_message(client, userdata, msg):
        if msg.topic == TOPIC_MODE:
            try:
                driver["d"].set_mode(json.loads(msg.payload).get("mode"))
            except (ValueError, AttributeError):
                # Tolerate a bare string payload as well as {"mode": ...}
                driver["d"].set_mode(msg.payload.decode(errors="replace").strip())

    def loop(client, stop):
        d = MidiDriver(client)
        driver["d"] = d
        parser = MidiParser()
        fd = None
        while not stop.is_set():
            if fd is None:
                try:
                    fd = os.open(MIDI_DEVICE, os.O_RDONLY | os.O_NONBLOCK)
                    log(f"reading {MIDI_DEVICE}; notes "
                        f"{MIDI_BASE_NOTE}..{MIDI_BASE_NOTE + len(NOTE_VALVES) - 1} "
                        f"-> {', '.join(NOTE_VALVES)}")
                except OSError as e:
                    # Keyboard unplugged is normal, not fatal — keep waiting.
                    log(f"waiting for {MIDI_DEVICE}: {e}")
                    stop.wait(5)
                    continue
            try:
                r, _, _ = select.select([fd], [], [], 0.25)
                if not r:
                    continue
                chunk = os.read(fd, 256)
                if not chunk:
                    continue
                for status, d1, d2 in parser.feed(chunk):
                    d.handle(status, d1, d2)
            except OSError as e:
                log(f"MIDI read error, reopening: {e}")
                try:
                    os.close(fd)
                except OSError:
                    pass
                fd = None
                stop.wait(2)
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass

    run_mqtt_service("midi", [TOPIC_MODE], on_message, background_loop=loop)


if __name__ == "__main__":
    main()

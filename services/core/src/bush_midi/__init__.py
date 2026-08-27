#!/usr/bin/env python3
"""
MIDI keyboard -> solenoids and lights.

Reads a raw ALSA rawmidi character device directly rather than going through
mido/rtmidi: MIDI is a byte protocol simple enough to parse inline, and this
avoids building a C extension on the board. The device is root:audio 0660, so
the service only needs the audio group.

Notes fire solenoids. Velocity sets the pulse length, so how hard you hit the
key is how long the valve stays open — that is the whole point of playing the
fire from a keyboard. Control changes drive the lights.

Only acts while bush/mode is "midi", so a keyboard left plugged in cannot
fire the rig when the installation is running its own pipeline.

Safety: every pulse is clamped to MIDI_MAX_MS here, and the firmware clamps
again at MAX_PULSE_MS. Note-off does not close the valve — the firmware's
own timer does. A dropped note-off can therefore never hold a solenoid open.
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

# Velocity 1..127 maps onto this range. The floor keeps a soft touch from
# producing a pulse too short to light, the ceiling is the safety clamp.
MIDI_MIN_MS = int(os.environ.get("MIDI_MIN_MS", "60"))
MIDI_MAX_MS = int(os.environ.get("MIDI_MAX_MS", "1200"))

# Poof is a short, sharp effect; its own ceiling keeps a hard keypress from
# turning it into a long burn.
POOF_MAX_MS = int(os.environ.get("MIDI_POOF_MAX_MS", "450"))

# Ignore a repeated note-on inside this window — cheap protection against a
# stuck key or an over-enthusiastic tremolo emptying the propane.
RETRIGGER_MS = int(os.environ.get("MIDI_RETRIGGER_MS", "120"))

# ── control changes -> lights ──────────────────────────────────────────────
# CC number -> (topic, low, high). The VI25's knobs default to CC 20-27.
CC_BRIGHTNESS = int(os.environ.get("MIDI_CC_BRIGHTNESS", "1"))    # mod wheel
CC_LAYOUT     = int(os.environ.get("MIDI_CC_LAYOUT", "20"))       # first knob

# MIDI status bytes
NOTE_OFF, NOTE_ON, CONTROL_CHANGE = 0x80, 0x90, 0xB0


def scale_velocity(velocity: int, valve: str) -> int:
    """Velocity 1-127 -> pulse ms, clamped per valve type."""
    frac = max(0, min(127, velocity)) / 127.0
    ms = int(MIDI_MIN_MS + frac * (MIDI_MAX_MS - MIDI_MIN_MS))
    if valve.startswith("poof"):
        ms = min(ms, POOF_MAX_MS)
    return max(MIDI_MIN_MS, min(ms, MIDI_MAX_MS))


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

    # ── mode gate ──────────────────────────────────────────────────────────
    def set_mode(self, mode):
        if mode != self.mode:
            log(f"mode: {self.mode} -> {mode}")
        self.mode = mode

    @property
    def armed(self) -> bool:
        return self.mode == "midi"

    # ── handlers ───────────────────────────────────────────────────────────
    def handle(self, status, d1, d2):
        kind = status & 0xF0
        if kind == NOTE_ON and d2 > 0:
            self.note_on(d1, d2)
        elif kind == CONTROL_CHANGE:
            self.control_change(d1, d2)
        # NOTE_OFF (and NOTE_ON with velocity 0) deliberately ignored: the
        # firmware closes the valve on its own timer, so a lost note-off
        # cannot strand a solenoid open.

    def note_on(self, note, velocity):
        idx = note - MIDI_BASE_NOTE
        if idx < 0 or idx >= len(NOTE_VALVES):
            return
        valve = NOTE_VALVES[idx]
        now = time.monotonic()
        if (now - self._last_fire.get(valve, 0.0)) * 1000 < RETRIGGER_MS:
            return
        self._last_fire[valve] = now

        if not self.armed:
            log(f"note {note} -> {valve} ignored (mode={self.mode})")
            return
        ms = scale_velocity(velocity, valve)
        self.client.publish(TOPIC_FLAME, json.dumps({"valve": valve, "ms": ms}))
        log(f"note {note} vel {velocity} -> {valve} {ms}ms")

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

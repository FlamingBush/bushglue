#!/usr/bin/env python3
"""
MIDI keyboard -> solenoids and lights.

Notes hold solenoids open: key down opens the valve, key up closes it, so the
length of the note is the length of the flame. Control changes drive the
lights.

Two input backends, because the keyboard is not always plugged into the rig:

  rawmidi  the ALSA rawmidi character device, read and parsed inline rather
           than through mido/rtmidi -- MIDI is a byte protocol simple enough
           to parse here, and this avoids building a C extension on the board.
           The device is root:audio 0660, so the service only needs the audio
           group. Default on Linux.
  rtmidi   python-rtmidi, which talks WinMM on Windows and CoreMIDI on macOS.
           Default off Linux; there is no /dev/snd there and select() only
           works on sockets, so the rawmidi path cannot run.

Either way the driver below sees the same (status, d1, d2) triples.

Off the rig, set BUSH_MQTT_BROKER to the broker's address -- a laptop cannot
guess it.

Only acts while bush/mode is "midi", so a keyboard left plugged in cannot
fire the rig when the installation is running its own pipeline.

Safety: a key-down opens the valve for its maximum hold, not indefinitely, and
the key-up closes it early. If a key-up is ever lost -- a dropped packet, a
yanked cable, a laptop that goes to sleep, the service dying mid-note -- the
firmware's own timer still closes the valve at that maximum. Nothing here can
hold a solenoid open forever, which is why the open is expressed as a bounded
pulse rather than a latch.
"""
import glob
import json
import os
import queue
import sys
import time

from bushutil import make_logger, run_mqtt_service

log = make_logger("midi")

# ── input backend ──────────────────────────────────────────────────────────
# auto = rawmidi on Linux, rtmidi elsewhere. Force one with MIDI_BACKEND.
MIDI_BACKEND = os.environ.get("MIDI_BACKEND", "auto").strip().lower()

# rawmidi: default matches the Alesis VI25 on this rig; `amidi -l` lists the
# alternatives.
MIDI_DEVICE = os.environ.get("MIDI_DEVICE", "/dev/snd/midiC4D0")

# rtmidi: case-insensitive substring of the input port name ("VI25", "MPK").
# Empty means the first port offered, which on Windows is usually the only
# one -- the built-in GS Wavetable Synth is an output, so it does not appear.
MIDI_PORT = os.environ.get("MIDI_PORT", "").strip()

# ── topics ─────────────────────────────────────────────────────────────────
TOPIC_FLAME      = "bush/flame/pulse"
TOPIC_MODE       = "bush/mode"
TOPIC_BRIGHTNESS = "bush/lights/brightness"
TOPIC_LAYOUT     = "bush/lights/layout"

# ── note -> valve ──────────────────────────────────────────────────────────
# White keys only, walking up from MIDI_BASE_NOTE, in this order. Black keys
# do nothing at all: seven naturals in a row are found by feel rather than by
# looking, which matters when the thing you are aiming at is on fire.
#
# 62 (D4) is the *right* end of the 25-key board (which spans 48..72), so the
# seven valve keys are the top seven naturals -- D E F G A B C -- sitting under
# the right hand with the left half of the board free. C3 = 48 by the
# convention CircuitPython and most DAWs use; if the controller's octave
# buttons get bumped, shift MIDI_BASE_NOTE by whole octaves rather than
# rewiring anything.
MIDI_BASE_NOTE = int(os.environ.get("MIDI_BASE_NOTE", "62"))
NOTE_VALVES = [v for v in os.environ.get(
    "MIDI_VALVES",
    "flare1,flare2,flare3,bigjet1,bigjet2,bigjet3,poof1").split(",") if v.strip()]

# C D E F G A B — the pitch classes with no sharp below them.
WHITE_PITCH_CLASSES = (0, 2, 4, 5, 7, 9, 11)


def white_keys_from(base: int, count: int) -> list:
    """The first *count* white keys at or above *base*.

    A base that lands on a black key simply starts at the next natural, so an
    octave shift never has to be talked out of an accidental.
    """
    notes, n = [], base
    while len(notes) < count and n <= 127:
        if n % 12 in WHITE_PITCH_CLASSES:
            notes.append(n)
        n += 1
    return notes


VALVE_NOTES = white_keys_from(MIDI_BASE_NOTE, len(NOTE_VALVES))
NOTE_MAP = dict(zip(VALVE_NOTES, NOTE_VALVES))

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
        return NOTE_MAP.get(note)

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


# ── input sources ──────────────────────────────────────────────────────────
# Both present the same interface: .label, .messages(timeout) -> list of
# (status, d1, d2), .alive(), .close(). Anything that means "the keyboard is
# gone" raises OSError from messages() so the run loop reopens.

class RawMidiSource:
    """ALSA rawmidi character device, parsed inline. Linux only."""

    def __init__(self, path=None):
        # select() is imported here so the module still imports on Windows,
        # where select exists but cannot watch a character device.
        import select
        self._select = select
        self.label = path or MIDI_DEVICE
        self._parser = MidiParser()
        self._fd = os.open(self.label, os.O_RDONLY | os.O_NONBLOCK)

    def messages(self, timeout):
        r, _, _ = self._select.select([self._fd], [], [], timeout)
        if not r:
            return []
        try:
            chunk = os.read(self._fd, 256)
        except BlockingIOError:
            return []
        return list(self._parser.feed(chunk)) if chunk else []

    def alive(self):
        # A vanished device surfaces as a read error, which is enough.
        return True

    def close(self):
        try:
            os.close(self._fd)
        except OSError:
            pass

    @staticmethod
    def ports():
        return sorted(glob.glob("/dev/snd/midi*"))


class RtMidiSource:
    """python-rtmidi input port: WinMM on Windows, CoreMIDI on macOS."""

    def __init__(self, match=None):
        import rtmidi
        match = MIDI_PORT if match is None else match
        self._in = rtmidi.MidiIn()
        ports = self._in.get_ports()
        if not ports:
            self._in.delete()
            raise OSError("no MIDI input ports")
        idx = _match_port(ports, match)
        if idx is None:
            self._in.delete()
            raise OSError(f"no MIDI input port matching {match!r}; "
                          f"have {', '.join(ports)}")
        self.label = ports[idx]
        # SysEx, clock and active-sensing are noise here, and dropping them in
        # the C layer keeps them out of the queue entirely.
        self._in.ignore_types(sysex=True, timing=True, active_sense=True)
        self._q = queue.Queue()
        # rtmidi delivers on its own thread; the queue hands messages to the
        # run loop so publishing stays on one thread.
        self._in.set_callback(lambda msg_dt, _: self._q.put(msg_dt[0]))
        self._in.open_port(idx)

    def messages(self, timeout):
        out = []
        try:
            out.append(self._q.get(timeout=timeout))
        except queue.Empty:
            return []
        while True:                      # drain whatever else arrived
            try:
                out.append(self._q.get_nowait())
            except queue.Empty:
                break
        # Two-byte messages (program change, channel pressure) are not
        # anything the driver acts on.
        return [tuple(m[:3]) for m in out if len(m) >= 3]

    def alive(self):
        # Unplugging on Windows silently stops the callback rather than
        # erroring, so watch the port list instead.
        try:
            return self.label in self._in.get_ports()
        except Exception:
            return False

    def close(self):
        try:
            self._in.cancel_callback()
            self._in.close_port()
            self._in.delete()
        except Exception:
            pass

    @staticmethod
    def ports():
        import rtmidi
        m = rtmidi.MidiIn()
        try:
            return m.get_ports()
        finally:
            m.delete()


def _match_port(ports, match):
    """Index of the first port containing *match*, or 0 when match is empty."""
    if not match:
        return 0 if ports else None
    lowered = match.lower()
    for i, name in enumerate(ports):
        if lowered in name.lower():
            return i
    return None


def backend():
    """Which source class to use."""
    if MIDI_BACKEND in ("rawmidi", "raw", "alsa"):
        return RawMidiSource
    if MIDI_BACKEND in ("rtmidi", "rt"):
        return RtMidiSource
    return RawMidiSource if sys.platform.startswith("linux") else RtMidiSource


def list_ports():
    cls = backend()
    ports = cls.ports()
    print(f"backend: {cls.__name__}")
    if not ports:
        print("  (no MIDI inputs found)")
    for p in ports:
        print(f"  {p}")


def main():
    if "--list" in sys.argv[1:]:
        list_ports()
        return

    driver = {}
    source_cls = backend()

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
        source = None
        while not stop.is_set():
            if source is None:
                try:
                    source = source_cls()
                    log(f"reading {source.label} via {source_cls.__name__}; "
                        f"white keys {VALVE_NOTES[0]}..{VALVE_NOTES[-1]} "
                        f"-> {', '.join(NOTE_VALVES)}")
                except Exception as e:
                    # Keyboard unplugged is normal, not fatal — keep waiting.
                    log(f"waiting for a MIDI input: {e}")
                    stop.wait(5)
                    continue
            try:
                for status, d1, d2 in source.messages(0.25):
                    d.handle(status, d1, d2)
                if not source.alive():
                    raise OSError("input port disappeared")
            except Exception as e:
                log(f"MIDI read error, reopening: {e}")
                # Whatever we were holding, stop holding it: the firmware
                # ceiling would close it anyway, but not until it expires.
                d.release_all()
                source.close()
                source = None
                stop.wait(2)
        if source is not None:
            source.close()

    run_mqtt_service("midi", [TOPIC_MODE], on_message, background_loop=loop)


if __name__ == "__main__":
    main()

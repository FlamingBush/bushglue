# Relay Control Board

## Hardware

| Field | Value |
|---|---|
| Board | Raspberry Pi Pico 2 W |
| Board ID | `raspberry_pi_pico2_w` |
| CircuitPython | 10.0.3 (2025-10-17) |

## Pin Assignments

| Pin | Channel | Default valve |
|---|---|---|
| GP6  | 0 | flare1 |
| GP7  | 1 | flare2 |
| GP8  | 2 | flare3 |
| GP9  | 3 | bigjet1 | *(pre-existing poof channel, reused)* |
| GP10 | 4 | bigjet2 |
| GP11 | 5 | bigjet3 |
| GP12 | 6 | poof1 |

Six of these are new drivers; GP9 already carried the original poof relay.
GP2 and GP3 (the old flare/bigjet channels) are no longer used.

**Channel index is the position in `OUTPUT_PINS`, not the GPIO number** —
channel 0 is GP6. `bush/flame/identify` and `bush-valve-id` both address
channels by that index.

The "default valve" column is only what the firmware assumes when no map has
been published. The loom is assembled before anyone knows which solenoid
landed on which channel, so the real mapping is discovered after the fact with
`bush-valve-id` and published retained on `bush/flame/map`.

## MQTT Topics

| Topic | Direction | Payload | Cadence / Effect |
|---|---|---|---|
| `bush/flame/pulse` | sub | `{"valve":"bigjet2","ms":350}` | Fire one named valve for N ms. Valves are individually addressed: `flare1..3`, `bigjet1..3`, `poof1`. A bare type name (`bigjet`) is **not** a valve and is rejected — the firmware never guesses which of three to fire. Pulses are capped at `MAX_PULSE_MS` (10 s) |
| `bush/flame/identify` | sub | `{"out":3,"ms":250}` | Fire a **raw channel**, ignoring the map. Used to discover the wiring after assembly |
| `bush/flame/map` | sub | `{"flare1":0,"bigjet2":4,...}` | **Retained.** Replaces the name→channel map wholesale. Rejected as a unit if malformed, so a half-applied map can never fire the wrong valve. All outputs are forced off before a remap takes effect |
| `bush/flame/status` | pub | JSON: `{ticks_ms, outputs[], valves{}, map{}}` | Liveness beacon, **every 5 s** (`STATUS_INTERVAL_MS`); also published immediately on (re)connect. Carries the live map so commissioning tools can read back what the board actually believes. Deploy verification waits on this |

The needle valve is NOT on this board — it's the CAN-based fleet in `firmware/valve-control/` (`bush/fire/valve/*` topics).

## Required CircuitPython Libraries

Install from the [CircuitPython Library Bundle](https://circuitpython.org/libraries)
matching CircuitPython 10.x:

- `adafruit_minimqtt` (folder)
- `adafruit_requests.mpy`
- `adafruit_connection_manager.mpy`
- `adafruit_ticks.mpy`

Copy the above into `CIRCUITPY/lib/`.

TODO bundle these

## Firmware Files (CIRCUITPY root)

| File | Purpose |
|---|---|
| `code.py` | **Active firmware** — non-blocking MQTT GPIO pulse controller |
| `secrets.py` | WiFi + MQTT credentials (copy from `secrets.example.py`, do not commit) |

## Rebuild Steps

1. Flash CircuitPython 10.x onto the Pico 2 W.
2. Copy all files from `CIRCUITPY/` in this package to the `CIRCUITPY` drive.
3. Copy `secrets.example.py` to `secrets.py` and fill in credentials.
4. Install libraries listed above into `CIRCUITPY/lib/`.
5. The board auto-starts `code.py` on power-up.

## MIDI keyboard

`bush-midi` reads a raw ALSA rawmidi device (an Alesis VI25 here, `amidi -l`
lists others) and plays the solenoids from the keys. Seven consecutive
semitones from `MIDI_BASE_NOTE` (default 48 = C3) map onto the seven valves in
`MIDI_VALVES` order. Velocity sets the pulse length, so how hard you hit the
key is how long the valve opens.

It only fires while `bush/mode` is `midi`, so a keyboard left plugged in
cannot fire the rig during a normal show. Note-off is deliberately ignored —
the firmware's own timer closes the valve, so a dropped note-off can never
strand a solenoid open. Knobs (CC) drive the lights in any mode.

## Poofer fallback

`bush/flame/poof-fallback` (retained) substitutes **all three bigjets, fired
together** for any poof request. It lives in the firmware so every publisher
inherits it at once — the web UI, `bush-midi`, `bush-sentiment` and
`bush-firecontrol` — rather than each needing its own copy of the rule. The
substitute set is read from the live valve map, so re-running `bush-valve-id`
does not invalidate it. State is echoed in `bush/flame/status`.

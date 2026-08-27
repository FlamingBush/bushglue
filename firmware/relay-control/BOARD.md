# Relay Control Board

## Hardware

| Field | Value |
|---|---|
| Board | Raspberry Pi Pico 2 W |
| Board ID | `raspberry_pi_pico2_w` |
| CircuitPython | 10.0.3 (2025-10-17) |

## Pin Assignments

| Channel | Pin |
|---|---|
| 0 | GP4 |
| 1 | GP5 |
| 2 | GP6 |
| 3 | GP7 |
| 4 | GP8 |
| 5 | GP10 |
| 6 | GP11 |
| 7 | GP12 |

Eight relay drivers for seven solenoids, so one channel is spare.

**GP9 is not connected** and is deliberately absent from `OUTPUT_PINS`. It
used to carry the old poof relay. Leaving it in the list cost an afternoon of
"why is there no click on bigjet1" — which was only ever a valve name aimed at
a dead pin.

**Channel index is the position in `OUTPUT_PINS`, not the GPIO number.**
`bush/flame/identify` and `bush-valve-id` both address channels by that index,
so channel 0 is GP4 and channel 7 is GP12.

The name→channel map is discovered after assembly with `bush-valve-id` and
published retained on `bush/flame/map`. The firmware's built-in default is a
placeholder, not a description of the wiring.

## MQTT Topics

| Topic | Direction | Payload | Cadence / Effect |
|---|---|---|---|
| `bush/flame/pulse` | sub | `{"valve":"bigjet2","ms":350}` | Fire one named valve for N ms. **`ms: 0` closes that valve immediately** — a held MIDI key opens for its maximum and the key-up closes it early. Valves are individually addressed: `flare1..3`, `bigjet1..3`, `poof1`. A bare type name (`bigjet`) is **not** a valve and is rejected — the firmware never guesses which of three to fire. Pulses are capped at `MAX_PULSE_MS` (10 s) |
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

Keys are **hold-to-fire**: key down opens the valve, key up closes it, so the
length of the note is the length of the flame. The open is expressed as a
bounded pulse rather than a latch — 5 s for the jets, 1 s for the poofer — so
a lost key-up (dropped packet, yanked cable, service killed mid-note) still
ends with the firmware closing the valve on its own timer. Leaving `midi` mode
mid-note releases everything held.

It only fires while `bush/mode` is `midi`, so a keyboard left plugged in
cannot fire the rig during a normal show. Knobs (CC) drive the lights in any
mode.

**Wi-Fi power save is disabled at boot.** The CYW43 defaults to
`PowerManagement.MIN`, which parks the radio between beacons and added 60-100
ms to every command — plainly audible when playing the solenoids. Measured
round trip publish->act->report: 160 ms median before, 7 ms after.

## Poofer fallback

`bush/flame/poof-fallback` (retained) substitutes **all three bigjets, fired
together** for any poof request. It lives in the firmware so every publisher
inherits it at once — the web UI, `bush-midi`, `bush-sentiment` and
`bush-firecontrol` — rather than each needing its own copy of the rule. The
substitute set is read from the live valve map, so re-running `bush-valve-id`
does not invalidate it. State is echoed in `bush/flame/status`.

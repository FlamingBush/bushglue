# bush-cue music bring-up (for a fresh Claude)

Goal: take the audio→valve/flame feature to a real music show on the **current hardware**:
a **Pico 2 W valve node** driving an **MKS SERVO42D as a plain stepper over STEP/DIR**, commanded
over Wi-Fi/MQTT. You (the new Claude) have **no prior session context** — read this, the linked
memories, and the code before touching hardware. Work the phases in order; each proves one thing
first.

> Hardware history (so old notes make sense): this started on a XIAO nRF52840 over **BLE**, then a
> Pico 2 W over UART, then a Pico 2 W over **CAN** (MCP2515), and is now a Pico 2 W driving the 42D
> as a **plain stepper over STEP/DIR** — CAN was abandoned when the MCP2515 controller died
> (2026-06-06). The BLE/UART/CAN firmware + the `bush_valve_ble` MQTT↔BLE bridge are in git history
> but are NOT used here. The Android app's BLE path can't reach a Wi-Fi Pico — it's only useful as a
> remote (ssh-convert + ssh-trigger the odroid).

## What exists (on `main`)

`bush-cue` turns a music file into synchronized **valve** + **flame** cues:
- **Engine** `services/audio/src/bush_cue/` (Python, odroid). `bush-cue analyze` (ffmpeg→STFT→
  energy/bands/onset/tempo/beats→cue sheet) and `bush-cue play` (owns the audio clock; streams
  the valve waveform ahead of the playhead as MQTT frames; flame with a latency-lead). Installed
  at `~/bushglue/.venv/bin/bush-cue`.
- **Firmware** `firmware/valve-control/` (Pico 2 W, CircuitPython). **Wi-Fi/MQTT** to the odroid
  (the proven hand-rolled MQTT client in `code.py`) plus **STEP/DIR** pulses straight to the
  SERVO42D (`valve.py` — `pwmio` PWM frequency = steps/sec, no CAN/UART/encoder). It subscribes to
  `bush/fire/valve/*` and `bush/fire/valve/stream` (binary waveform frames, sentinel `0xF5`),
  buffers the stream and plays each sample on its own `ticks_ms` clock (open-loop — the host
  waveform IS the position), and publishes telemetry (`actual`/`status`/`streampos`/`pong`). The
  onboard LED shows a coarse on/off actuation level (the Pico W LED is single-colour/digital — no
  RGB/brightness).
- **No bridge, no BLE, no CAN.** The Pico is the MQTT client directly, so `bush-cue play`'s stream
  goes odroid → MQTT → Pico → step/dir → motor with nothing in between.

## Read first
- `firmware/valve-control/PROTOCOL.md` — **step/dir section** (STEP/DIR/EN, pwmio velocity,
  dead-reckoned position, 42D menu, GP4/GP5 wiring).
- `plans/servo42d-bringup.md` — the 42D motor bench plan over step/dir (jog → direction → breath →
  calibrate; no homing). **Do that first**; this plan assumes a working, calibrated valve.
- Memories (verify vs code): **project_xiao_valve_node** (now Pico step/dir),
  **project_valve_mks_quirks**, **project_valve_supply_current**, **project_valve_closed_seat_safety**,
  **project_bush_cue_engine**.
- Code: `bush_cue/{analyze,play,features,mapping,presets,safety,wire,cuesheet}.py`,
  `firmware/valve-control/CIRCUITPY/{code.py,valve.py}`.

## Deploying code to hardware
- **Pico firmware:** copy `firmware/valve-control/CIRCUITPY/{valve.py,code.py}` onto the Pico's
  mounted `CIRCUITPY` drive (restarts on write); create `secrets.py` from `secrets.example.py`
  (Wi-Fi + `MQTT_BROKER` = the odroid). **No extra CircuitPython libs needed for the motor** —
  step/dir is pure `pwmio` + `digitalio`. Pin block at the top of `code.py`: `PIN_STEP=board.GP4`,
  `PIN_DIR=board.GP5` (reuse the old UART wiring; avoid GP2/GP3 = relay pins), `valve.en=None`
  (EN unwired → 42D `En=Hold`). The 42D must be on **Mode=CR_vFOC, MStep=16, En=Hold**; run current
  is a 42D menu setting.
- **odroid (engine):** `git push`, then on the odroid `git pull && ~/.local/bin/uv sync
  --all-packages` (or the `deploy` skill) so `~/bushglue/.venv/bin/bush-cue` picks up edits.

## Golden rules
- **Power: 12–24 V → SERVO42D `V+`/`GND`, never `IN_*`** (limit inputs). Check polarity — reverse
  power killed the 42C. **Common ground** between the Pico and the driver. Wiring: Pico GP4→42D
  STP, GP5→DIR, pulse-port COM→3.3 V.
- **Flame is real propane.** Start every run `--no-flame`. Only arm flame when the operator
  confirms it's safe to fire. Be conservative via the SHEET (analyze with `swell`, fewer flame
  channels, or a lower `--max-cue-rate`) — `bush-cue play` has no rate knob; its only play-time
  levers are `--no-flame` and killing the publisher.
- **The closed seat is force-sensitive** — `pos_min` must stay above the seat; never command a
  full-speed stroke into the seat. (project_valve_closed_seat_safety)
- **There is no homing** (step/dir build has no feedback): boot and `home` set position 0 to the
  **current shaft position**, so `[pos_min, pos_max]` are relative to wherever it powered up, NOT
  the real seat. Don't trust seat safety from position alone — keep the valve uncoupled on the
  bench, or hand-park it mid-travel before powering on. Phase B has no homing step.
- **E-stop:** publish `bush/fire/valve/stop` (bush-monitor / `mosquitto_pub`) or kill `bush-cue play`.

## Phase A — bring the Pico node up
1. Flash per "Deploying" above. Read the Pico's serial console (`/dev/cu.usbmodem*` / `/dev/ttyACM*`).
2. Expect it to join Wi-Fi + MQTT (the hand-rolled client logs "MQTT connected") and the line
   `Valve(42D step/dir): init -- boot position = 0, ready`. There's no motor handshake — init can't
   fail on the motor link (step/dir is open-loop). The MQTT loop runs whether or not the motor is
   powered. (VERIFIED 2026-06-06: boots clean with no motor attached, joins "Glass House" Wi-Fi,
   reaches the odroid broker at 192.168.86.29.)
3. Sanity: `mosquitto_sub -v -t 'bush/fire/valve/#'` on the odroid; you should see `status`/`actual`.

## Phase B — validate + calibrate the 42D (step/dir)
Run **`plans/servo42d-bringup.md` Phases 0–4**: step/dir jog, direction sense (VERIFIED toward open
2026-06-06 — flip `DIR_OPEN_LEVEL` in `valve.py` if a move goes the wrong way), breath, **no homing
(N/A — no feedback)**, and **calibrate `OPEN_STEPS`** (placeholder 2000; set real travel via
`bush/fire/valve/calibrate <steps>`). Confirm `target` 0.0/0.5/1.0 land where expected. There is no
encoder/stall path to validate, and no `maxtorque` (run current is on the 42D menu).

## Phase C — validate streamed playback sync (the core of this feature)
The valve is driven by a dense position waveform played on the firmware clock. The only thing that
emits stream frames is a real (non-dry-run) `bush-cue play` of an analyzed sheet — `--dry-run` only
PRINTS the schedule, and there's no standalone test-stream generator. No bridge: the Pico subscribes
to `bush/fire/valve/stream` over MQTT directly.

1. **Open-loop telemetry check** (no motor needed): make a sheet from any short clip
   (`bush-cue analyze CLIP --preset swell -o /tmp/s.json`); then on the odroid, in one shell
   `mosquitto_sub -v -t 'bush/fire/valve/streampos' -t 'bush/fire/valve/pong'` and in another
   `bush-cue play /tmp/s.json --no-flame` (no `--audio` — it streams off its own monotonic clock).
   Confirm `streampos <play_ms> <pos>` advances and tracks the sheet's waveform, staying aligned over
   minutes (the `pong` exchange anchors the clock). The Pico LED should toggle with the level.
2. **With the motor** (after Phase B): confirm the needle follows the waveform and never slams the
   seat; watch the LED and `bush/fire/valve/actual`.

## Phase D — end-to-end music, valve-only (no fire)
1. **Get a real audio file.** The odroid's `~/robotrock-sFZjqVnWBhc.mp4` is **AV1 video with no
   audio track** — useless; use an actual audio/music file.
2. On the odroid: `bush-cue analyze TRACK --preset pulse -o /tmp/sheet.json` (presets: `swell` =
   valve-only, `pulse` = beats, `drama` = big reveals).
3. `bush-cue play /tmp/sheet.json --audio TRACK --no-flame` — plays audio on the odroid and streams
   the valve over MQTT → Pico → step/dir → motor. Trim `--latency-lead-ms` until it feels on-beat.
   (Optional: the Android app can ssh-convert and ssh-trigger this remotely; its BLE path is unused.)

## Phase E — add flame (careful)
This Pico is **valve-only**; flame relays (`bush/flame/pulse`) live on a separate **relay-control**
board. Bring that up (its own firmware) and confirm it's safe to fire, then:
`bush-cue play /tmp/sheet.json --audio TRACK` (flame on). Confirm poofs land on-beat, the duty cap
holds, nothing runs away. Safety (per-channel ms ceilings, refractory, bigjet min-gap, rolling duty
budget) is enforced in `bush_cue/safety.py` and re-asserted at play time.

## Phase F — tune & finalize
- Tune the targeted knobs (re-`analyze` to apply): sensitivity = `--gain --onset-threshold
  --energy-gate --tone-tilt --agc`; output range/style = `--pos-min --pos-max --stroke-depth
  --motion-smoothing --tempo-lock/--tempo-multiplier --max-cue-rate --channels`.
- Once validated on hardware: update the "uncalibrated" caveats in memories (project_xiao_valve_node,
  project_valve_mks_quirks, project_bush_cue_engine) and the valve status in `CLAUDE.md` once
  `OPEN_STEPS` is set and travel/breath are confirmed under load.

## Gotchas
- **42D menu is the usual culprit:** must be **Mode=CR_vFOC** (pulse interface — a serial/CAN mode
  ignores the pulses), **MStep=16** (match `valve.MICROSTEP`), **En=Hold** (EN is unwired). Confirm
  these and `COM→3.3 V` + common ground + STP/DIR not swapped first.
- **No bridge / no BLE / no CAN:** don't try to run `bush_valve_ble` for this node, and the Android
  app's BLE streaming can't reach it. The stream is MQTT-direct to the Pico.
- **No feedback → silent drift:** position is dead-reckoned from the step rate. A wrong `OPEN_STEPS`
  or dropped steps under load skews position with no error. Watch the needle vs `actual` over a long
  run; calibrate `OPEN_STEPS` (Phase B) before trusting fractions.
- `bush-cue` is installed on the odroid (`~/bushglue/.venv/bin/bush-cue`); `ffmpeg` is present.

## Definition of done
Pico joins Wi-Fi/MQTT and boots clean (no motor handshake on step/dir); 42D validated +
`OPEN_STEPS` calibrated; stream sync confirmed via `streampos`; a real track plays with the valve
following in sync (and, when a relay-control board is up and armed, flame on-beat within the duty
cap); presets/knobs tuned; the "uncalibrated" caveats cleared.

# bush-cue music bring-up (for a fresh Claude)

Goal: take the audio→valve/flame feature to a real music show on the **current hardware**:
a **Pico 2 W valve node** driving an **MKS SERVO42D over CAN**, commanded over Wi-Fi/MQTT.
You (the new Claude) have **no prior session context** — read this, the linked memories, and
the code before touching hardware. Work the phases in order; each proves one thing first.

> Hardware history (so old notes make sense): this started on a XIAO nRF52840 over **BLE**,
> then moved to a Pico 2 W over UART, and is now a Pico 2 W over **CAN** (the motor is the
> SERVO42D **CAN** variant). The BLE/UART firmware + the `bush_valve_ble` MQTT↔BLE bridge are
> in git history but are NOT used here. The Android app's BLE path can't reach a Wi-Fi/CAN
> Pico — it's only useful as a remote (ssh-convert + ssh-trigger the odroid).

## What exists (on `main`)

`bush-cue` turns a music file into synchronized **valve** + **flame** cues:
- **Engine** `services/audio/src/bush_cue/` (Python, odroid). `bush-cue analyze` (ffmpeg→STFT→
  energy/bands/onset/tempo/beats→cue sheet) and `bush-cue play` (owns the audio clock; streams
  the valve waveform ahead of the playhead as MQTT frames; flame with a latency-lead). Installed
  at `~/bushglue/.venv/bin/bush-cue`.
- **Firmware** `firmware/valve-control/` (Pico 2 W, CircuitPython). Two buses: **Wi-Fi/MQTT** to
  the odroid (the proven hand-rolled MQTT client in `code.py`) and **CAN** to the SERVO42D (via
  an MCP2515; `valve.py`). It subscribes to `bush/fire/valve/*` and `bush/fire/valve/stream`
  (binary waveform frames, sentinel `0xF5`), buffers the stream and plays each sample on its own
  `ticks_ms` clock (open-loop — the host waveform IS the position), and publishes telemetry
  (`actual`/`status`/`streampos`/`pong`). The onboard LED shows a coarse on/off actuation level
  (the Pico W LED is single-colour/digital — no RGB/brightness).
- **No bridge, no BLE.** The Pico is the MQTT client directly, so `bush-cue play`'s stream goes
  odroid → MQTT → Pico → CAN → motor with nothing in between.

## Read first
- `firmware/valve-control/PROTOCOL.md` — **CAN section** (framing, CRC, 8-byte/24-bit-pulse, wiring).
- `plans/servo42d-bringup.md` — the 42D motor bench plan over CAN (comms → direction → breath →
  homing → calibrate). **Do that first**; this plan assumes a working, calibrated, homed valve.
- Memories (verify vs code): **project_xiao_valve_node** (now Pico/CAN), **project_valve_mks_quirks**,
  **project_valve_supply_current**, **project_valve_closed_seat_safety**, **project_bush_cue_engine**.
- Code: `bush_cue/{analyze,play,features,mapping,presets,safety,wire,cuesheet}.py`,
  `firmware/valve-control/CIRCUITPY/{code.py,valve.py}`.

## Deploying code to hardware
- **Pico firmware:** copy `firmware/valve-control/CIRCUITPY/{valve.py,code.py}` onto the Pico's
  mounted `CIRCUITPY` drive (restarts on write); create `secrets.py` from `secrets.example.py`
  (Wi-Fi + `MQTT_BROKER` = the odroid). Needs `circup install adafruit_mcp2515` (pulls
  `adafruit_bus_device`). Set the **CAN config block** at the top of `code.py`: SPI pins
  (default SCK/MOSI/MISO = GP6/GP7/GP4, CS GP5 — GP4/GP5 reuse the old UART wiring, GP4-GP7 are an
  SPI0 group; avoid GP2/GP3 = relay pins; a PiCowbell HAT forces GP18/19/16 + its own CS),
  `CAN_CRYSTAL` (16 MHz Adafruit/Waveshare, **8 MHz** generic blue module — wrong value = no comms),
  motor CAN ID (`valve.ADDR`, default 1).
- **odroid (engine):** `git push`, then on the odroid `git pull && ~/.local/bin/uv sync
  --all-packages` (or the `deploy` skill) so `~/bushglue/.venv/bin/bush-cue` picks up edits.

## Golden rules
- **Power: 12–24 V → SERVO42D `V+`/`GND`, never `IN_*`** (limit inputs). Check polarity — reverse
  power killed the 42C. **Common ground** between the Pico/MCP2515 and the driver; 120 Ω CAN
  termination at the bus ends.
- **Flame is real propane.** Start every run `--no-flame`. Only arm flame when the operator
  confirms it's safe to fire. Be conservative via the SHEET (analyze with `swell`, fewer flame
  channels, or a lower `--max-cue-rate`) — `bush-cue play` has no rate knob; its only play-time
  levers are `--no-flame` and killing the publisher.
- **The closed seat is force-sensitive** — `pos_min` must stay above the homed seat margin; never
  command a full-speed stroke into the seat. (project_valve_closed_seat_safety)
- **E-stop:** publish `bush/fire/valve/stop` (bush-monitor / `mosquitto_pub`) or kill `bush-cue play`.

## Phase A — bring the Pico node up
1. Flash per "Deploying" above. Read the Pico's serial console (`/dev/cu.usbmodem*` / `/dev/ttyACM*`).
2. Expect it to join Wi-Fi + MQTT (the hand-rolled client logs "MQTT connected") and `Valve(42D):
   init over CAN`. `init OK` proves the CAN link; `init FAILED` means the CAN link, not the motor —
   see PROTOCOL.md / servo42d-bringup Phase 0 (crystal_freq, termination, CAN ID, bitrate, common GND).
   If the motor isn't powered yet, `init FAILED` is expected and the MQTT loop still runs.
3. Sanity: `mosquitto_sub -v -t 'bush/fire/valve/#'` on the odroid; you should see `status`/`actual`.

## Phase B — validate + calibrate the 42D (over CAN)
Run **`plans/servo42d-bringup.md` Phases 0–4**: CAN comms/init, direction sense (CRITICAL — swap the
`DIR_TOWARD_OPEN/CLOSED` constants near the top of `valve.py` if a move goes the wrong way), breath
demo (`demo_breath.py`), stallguard homing into the seat, and **calibrate `OPEN_STEPS`** (source is
`2000 * _USTEP` = 2000 at 16× microstep; set real travel via `bush/fire/valve/calibrate <steps>`).
Confirm `target` 0.0/0.5/1.0 land where expected and the valve is homed. This exercises the motion
`VERIFY` values (direction, current, RPM, accel); `STREAM_MAX_RPM` is only exercised in Phase C.

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
   the valve over MQTT → Pico → CAN → motor. Trim `--latency-lead-ms` until it feels on-beat.
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
- Once validated on hardware: drop the `VERIFY` tags in `valve.py` for confirmed values, and update
  the "unvalidated" caveats in memories (project_xiao_valve_node, project_valve_mks_quirks,
  project_bush_cue_engine) and the valve status in `CLAUDE.md`.

## Gotchas
- **CAN config is the usual culprit:** wrong `crystal_freq` (8 vs 16 MHz silently uses the wrong
  bitrate), missing 120 Ω termination, wrong motor CAN ID, no common ground. Confirm these first.
- **No bridge / no BLE:** don't try to run `bush_valve_ble` for this node, and the Android app's
  BLE streaming can't reach it. The stream is MQTT-direct to the Pico.
- `bush-cue` is installed on the odroid (`~/bushglue/.venv/bin/bush-cue`); `ffmpeg` is present.
- `0xFD` over CAN uses a 24-bit pulse count (≤16.7 M) — fine at 16× microstep, but keep `OPEN_STEPS`
  /`HOME_MAX_PULSES` within it.

## Definition of done
Pico joins Wi-Fi/MQTT and `init OK` over CAN; 42D validated + `OPEN_STEPS` calibrated + homed; stream
sync confirmed via `streampos`; a real track plays with the valve following in sync (and, when a
relay-control board is up and armed, flame on-beat within the duty cap); presets/knobs tuned;
`VERIFY` tags and the "unvalidated" caveats cleared.

# bush-cue music bring-up (for a fresh Claude)

Goal: take the audio→valve/flame feature from "XIAO ground joint just got re-soldered" to a
real music show. You (the new Claude) have **no prior session context** — read this, the
linked memories, and the code before touching hardware. Work the phases in order; each proves
one thing before the next can hurt anything.

## What exists (all on `main`, commit `0dbddd1` or later)

`bush-cue` turns a music file into synchronized **valve** + **flame** cues. Built last session,
validated in software, **never validated on the motor**. Pieces:
- **Engine** `services/audio/src/bush_cue/` (Python, odroid). `bush-cue analyze` (ffmpeg→STFT→
  energy/bands/onset/tempo/beats→cue sheet) and `bush-cue play` (owns the audio clock; streams
  the valve waveform ahead of the playhead; flame with a latency-lead). Installed on the odroid
  at `~/bushglue/.venv/bin/bush-cue` (verified working via stdin).
- **Firmware** `firmware/valve-control/` (XIAO nRF52840, CircuitPython). New **stream mode**:
  the host streams a dense position waveform as binary BLE frames (sentinel `0xF5`); the firmware
  buffers them and plays each sample on its own `ticks_ms` clock (open-loop — the host waveform
  IS the position), so BLE jitter doesn't move motion timing. Onboard **RGB LED shows actuation
  level** (closed = faint blue, open = red/orange).
- **Bridge** `services/core/src/bush_valve_ble/` — MQTT↔BLE; passes `bush/fire/valve/stream`
  binary frames through to the XIAO and forwards telemetry back. Unit: `systemd/odroid/bush-valve-ble.service`.
- **Android** `apps/valve-ble-android/` — "Music → valve" screen: pick a track, SSH-convert on
  the odroid, play locally and stream the valve waveform over BLE (valve-only; flame is odroid-only).

## Read first
- `plans/servo42d-bringup.md` — the 42D motor bench plan (comms → direction → breath → homing →
  calibrate). **Do that first**; this plan assumes a working, calibrated, homed valve.
- `firmware/valve-control/PROTOCOL.md` — the 42D serial protocol.
- Auto-loaded memories (verify against code before trusting): **project_xiao_valve_node**,
  **project_valve_mks_quirks**, **project_valve_supply_current**, **project_valve_closed_seat_safety**,
  **project_bush_cue_engine**.
- Code: `bush_cue/{analyze,play,features,mapping,presets,safety,wire,cuesheet}.py`,
  `firmware/valve-control/CIRCUITPY/{code.py,valve.py}`, `bush_valve_ble/__init__.py`,
  `apps/.../MainActivity.java`.

## Deploying code to hardware
Two separate paths — the odroid deploy does NOT touch the XIAO:
- **XIAO firmware:** copy `firmware/valve-control/CIRCUITPY/{valve.py,code.py}` directly onto the
  board's mounted `CIRCUITPY` drive (CircuitPython restarts on write). Its REPL/console is a USB
  serial on the bench machine (`/dev/cu.usbmodem*` on a Mac). So every `valve.py` edit this plan
  calls for (swap `DIR_*`, set `OPEN_STEPS`, drop `VERIFY` tags) must be hand-copied to the board.
- **odroid (engine + bridge):** `git push`, then on the odroid `git pull && ~/.local/bin/uv sync
  --all-packages` (or run the `deploy` skill) so `~/bushglue/.venv/bin/{bush-cue,bush-valve-ble}`
  pick up your edits.

## Golden rules
- **Supply polarity before power** — reverse power killed the previous (42C) controller, and a
  miswired XIAO ground caused the brownout this plan starts after. Double-check the joint.
- **Flame is real propane.** Start every run `--no-flame`. Only arm flame when the operator
  confirms it's physically safe to fire. Be conservative via the SHEET (analyze with the `swell`
  preset, fewer flame channels, or a lower `--max-cue-rate`) — `bush-cue play` has no rate knob;
  its only play-time safety levers are `--no-flame` and killing the publisher.
- **The closed seat is force-sensitive** — `pos_min` must stay above the homed seat margin; never
  command a full-speed stroke into the seat. (project_valve_closed_seat_safety)
- **E-stop:** `bush/fire/valve/stop` (the app's STOP button). For flame, stop publishing / kill
  `bush-cue play` — flame safety lives only in the publisher (the relay firmware does NOT clamp).

## Phase A — confirm the ground fix (the thing that was broken)
1. Power the XIAO. Read its serial (`/dev/cu.usbmodem*` on a Mac, or `/dev/ttyACM*`/the mounted
   `CIRCUITPY` console on Linux).
2. **Pass signal = NO "Power dipped" safe mode**, plus `Valve node: BLE name bushvalve -- init` and
   the RGB LED lit (faint blue = closed). If it still drops into "Power dipped" safe mode → the
   joint is still bad; stop and report (it is NOT a USB-power or code issue).
   - `Valve(42D): init FAILED` (state=error) is **EXPECTED** here if the 42D motor isn't powered yet
     — `init()` blocks a few seconds then fails, but the BLE loop still runs, advertises, and lights
     the LED. "init OK" is not required for the ground check (it's proven in Phase B).
3. If `ImportError: adafruit_ble` → `/lib` is empty (happens after a CircuitPython reflash):
   `circup --path /Volumes/CIRCUITPY install adafruit_ble`. (Safe mode needs a HARD reset —
   `microcontroller.reset()` over the REPL or the physical button; CTRL-D won't clear it.)

## Phase B — validate + calibrate the 42D motor
Run **`plans/servo42d-bringup.md` Phases 0–4**: comms/init, direction sense (CRITICAL — swap the
`DIR_TOWARD_OPEN/CLOSED` constants near the top of `valve.py` if a move goes the wrong way), breath
demo, stallguard homing into the seat, and **calibrate `OPEN_STEPS`** (source is `2000 * _USTEP`,
= 2000 at the current 16× microstep; set the real travel via `bush/fire/valve/calibrate <steps>` or
edit `valve.py`). Confirm `target` 0.0/0.5/1.0 land where expected and the valve is homed. This
exercises the motion `VERIFY` values (direction, current, RPM, accel); `STREAM_MAX_RPM` is the one
`VERIFY` value only exercised later, in Phase C.

## Phase C — validate streamed playback sync (the core of this feature)
The valve is driven by a dense position waveform played on the firmware clock. Prove the sync before
music. **The only thing that emits stream frames is a real (non-dry-run) `bush-cue play` of an
analyzed sheet, or the Android app** — `--dry-run` only PRINTS the schedule (publishes nothing) and
there is no standalone test-stream generator.

**Where the bridge runs:** `bush-valve-ble` is the BLE central, so run it on the machine whose
Bluetooth is in range of the XIAO — the odroid if it has a working BLE adapter and is near the valve,
otherwise the bench Mac: `uv run --package bush-core bush-valve-ble` with `BUSH_MQTT_BROKER` set to
the odroid's mosquitto. Only one BLE central at a time — disconnect the phone first.

1. **Open-loop telemetry check** (no motor needed): make a sheet from any short clip
   (`bush-cue analyze CLIP --preset swell -o /tmp/s.json`); start the bridge; then in one shell
   `mosquitto_sub -v -t 'bush/fire/valve/streampos' -t 'bush/fire/valve/pong'` and in another
   `bush-cue play /tmp/s.json --no-flame` (no `--audio` — it streams off its own monotonic clock).
   Confirm `streampos <play_ms> <pos>` advances and tracks the sheet's waveform, and stays aligned
   over minutes (the `pong` exchange anchors the clock). The RGB LED should dance even with no motor.
2. **With the motor** (after Phase B): confirm the needle physically follows the waveform and never
   slams the seat; watch the LED and `bush/fire/valve/actual` track the level.

## Phase D — end-to-end music, valve-only (no fire)
1. **Get a real audio file.** The odroid's `~/robotrock-sFZjqVnWBhc.mp4` is **AV1 video with no
   audio track** — useless; use an actual audio/music file.
2. Analyze (on the odroid): `bush-cue analyze TRACK --preset pulse -o /tmp/sheet.json` (presets:
   `swell` = valve-only/BLE-safe, `pulse` = beats, `drama` = big reveals).
3. **Path 1 — Android preview:** app → Connect (`bushvalve`) → Pick track → set odroid host/user/
   password (tries `odroid-local`, falls back to `odroid`) → **Convert on odroid** → **Play**. The
   valve follows over BLE and the LED dances. Trim `--latency-lead-ms` until it feels on-beat.
4. **Path 2 — odroid player:** start the bridge (see Phase C for *where* it must run — it's the BLE
   central; the `bush-valve-ble.service` systemd unit is for the deployed location), then `bush-cue
   play /tmp/sheet.json --audio TRACK --no-flame`. Only one BLE central at a time — the phone OR the
   bridge, not both.

## Phase E — add flame (odroid only, careful)
1. Confirm the Pico flame relays (`bush/flame/pulse {valve,ms}`) are wired and it is safe to fire.
2. `bush-cue play /tmp/sheet.json --audio TRACK` (flame on). Confirm poofs land on-beat, the duty
   cap holds, and nothing runs away. Safety (per-channel ms ceilings, refractory, bigjet min-gap,
   rolling duty budget) is enforced in `bush_cue/safety.py` and re-asserted at play time.

## Phase F — tune & finalize
- Tune the targeted knobs (re-`analyze` to apply): sensitivity = `--gain --onset-threshold
  --energy-gate --tone-tilt --agc`; output range/style = `--pos-min --pos-max --stroke-depth
  --motion-smoothing --tempo-lock/--tempo-multiplier --max-cue-rate --channels`. Pick a preset and
  adjust from there.
- Once it's validated on hardware: drop the `VERIFY` tags in `valve.py` for confirmed values, and
  update the "unvalidated / no MKS yet" caveats in memories (project_xiao_valve_node,
  project_valve_mks_quirks, project_bush_cue_engine) and the valve status in `CLAUDE.md`.

## Gotchas this came from (last session)
- The "Power dipped" safe mode + CIRCUITPY drive unmounting was a **miswired XIAO ground** — fixed.
  If it recurs, re-check the joint, not the USB supply (there is no platform power issue).
- BLE advert: the 128-bit NUS UUID + name overflowed 31 bytes, so the node never advertised — fixed
  (name moved to the scan response, `code.py`). The host matches by name OR UUID, not a strict
  ServiceUuid filter. Don't reintroduce a hard UUID-only filter.
- `bush-cue` is installed on the odroid and reachable at `~/bushglue/.venv/bin/bush-cue`; `ffmpeg`
  is present.
- Android↔phone over wireless adb can drop; re-pair if `adb devices` goes empty.

## Definition of done
XIAO boots stable (no safe mode); 42D validated + `OPEN_STEPS` calibrated + homed; stream sync
confirmed via `streampos` telemetry; a real track plays with the valve following in sync (and,
when armed, flame on-beat within the duty cap); presets/knobs tuned; `VERIFY` tags and the
"unvalidated" caveats cleared.

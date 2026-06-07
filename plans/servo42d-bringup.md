# SERVO42D valve bring-up

Bench plan to validate and tune the 42D rewrite of `valve.py` once the new servo
arrives. The 42C was killed by reverse power (2026-06-04); the 42D driver is a full
rewrite (`firmware/valve-control/`) that **has never run on a 42D**. Everything tagged
`VERIFY` in `valve.py` is an estimate. Work the phases in order — each proves one thing
before the next can hurt anything. Reference: `firmware/valve-control/PROTOCOL.md`.

Golden rules for every phase:
- **Check supply polarity before applying power.** Reverse power is what killed the 42C.
- **Keep current low** (`VALVE_CURRENT_MA`, start ~300 mA) until homing is proven — current bounds the force into the seat.
- E-stop is `bush/fire/valve/stop` over MQTT (or cut power) — via bush-monitor, `mosquitto_pub`, or killing `bush-cue play`.

## Before you start

- **Transport is CAN** (the SERVO42D_CAN variant) on a **Pico 2 W + MCP2515** — the Pico has no native CAN. See PROTOCOL.md's CAN section.
- Wiring: 12–24 V → 42D **`V+`/`GND`** (NOT `IN_*`, those are limit inputs — check polarity, reverse power killed the 42C); 42D **`CAN_H`/`CAN_L`** → the MCP2515 transceiver; **common ground** between the Pico/MCP2515 and the 42D; **120 Ω termination** at the bus ends.
- Power the 42D, confirm the OLED is on and it can jog from its own menu (proves the servo is alive and not bricked).
- **Calibrate the encoder once, motor UNLOADED** (42D menu `Cal`, or `0x80`) before attaching the valve. Closed-loop position is garbage until this is done.
- Confirm 42D menu: **CAN bitrate 500 k**, **CAN ID 1**, motor type 1.8°. Work mode / microstep / current are set over CAN at init, so those don't need menu changes.
- On the Pico: CircuitPython + `circup install adafruit_mcp2515`. Set the CAN config (SPI pins, CS, `crystal_freq` — 16 MHz Adafruit/Waveshare, 8 MHz generic — and motor CAN ID) at the top of `code.py`.

## Phase 0 — comms only, no motion

Prove the CAN link + the init handshake before anything moves.

1. Easiest: copy `demo_breath.py` → `code.py` (it sets up the MCP2515 and runs init). Or from the REPL set up the bus like `code.py` does (`valve.can = CAN(...)`, `valve.Message = Message`) then `valve.init()`.
2. Expect `Valve(42D): init over CAN` → `init OK`. If `init FAILED`, it's the CAN link, not the motor.
3. `valve._blocking_read_encoder()` should return an int (not `None`) — confirms a CAN round-trip + CRC both ways.

If init fails, likely culprits in order: **wrong `crystal_freq`** (8 vs 16 MHz — silently wrong bitrate), missing **120 Ω termination**, wrong **motor CAN ID** (`valve.ADDR`), bitrate ≠ 500 k, `CAN_H`/`CAN_L` swapped or no common ground, or the 42D didn't accept `SR_vFOC` (`0x82 05`).

## Phase 1 — direction sense (CRITICAL, before any homing)

Homing drives `DIR_TOWARD_CLOSED` *into* the seat. If that bit is backwards, homing drives the wrong way. Learn the real sense first, motor **unloaded or backed well off the seat**.

1. Fake a home at mid-travel so moves are allowed (the `demo_breath.py` `_fake_home` does this), or just use `nudge` which allows motion in `unknown` state.
2. `nudge +5` (payload is **degrees**; + = toward closed) and `nudge -5` (toward open). Watch the shaft — 5° is a small jog, so use 30–45 if the direction is hard to see.
3. Confirm `+` physically corresponds to the valve **closing** and `-` to **opening**. If reversed, swap the `DIR_TOWARD_OPEN` / `DIR_TOWARD_CLOSED` constants near the top of `valve.py` (~line 85) and repeat.

Do not proceed to homing until this is confirmed.

## Phase 2 — breathing demo (no homing, no MQTT)

Validates init + `0x31` + `0xF6` speed mode + the breath oscillator on the bare motor.

1. Copy `demo_breath.py` → `code.py`. Watch the serial console.
2. Expect: init OK → faux-home seeded → "breathing now", then the shaft gently oscillates.
3. Tune for a smooth, visible breath: `MOVE_RPM`, `BREATH_ACC`, `BREATH_MAX_RPM`, and `DEMO_OPEN_STEPS` in the demo. If the motor doesn't move, RPM is rounding too low — raise `DEMO_OPEN_STEPS` or amplitude.
4. Confirms the 42D answers `0x31` at the valley (valley re-grounding works) — watch for `mks_silent` errors, which mean it isn't.

## Phase 3 — homing into the seat (stallguard)

Validate stall-sensed homing at low current/force. Attach the valve (or a soft stop standing in for the seat).

1. `VALVE_CURRENT_MA` low (~300 mA). Confirm protection is on (init sends `0x88 01`).
2. Send `home`. Watch the seek: it should drive toward the **closed** seat (Phase 1!), then stop on contact.
3. Confirm contact is detected — `0xFD status=0` (stall) is the primary signal; the `0x3E` read is the timeout backstop. Watch the contact force: it should be gentle.
4. Confirm the back-off toward open, the free-motion verify, and `0x92` zero. `motor_pos 0` must sit a margin **off** the seat, never resting on it.
5. Tune: lower `HOME_RPM`/`VALVE_CURRENT_MA` if it hits hard; raise `HOME_MAX_PULSES` if the seek errors as `home_no_contact` before reaching the seat (it must exceed full-open→seat travel).

Failure modes:
- Slams the seat hard → current/RPM too high.
- `home_no_contact` (drove full range, no stall) → `HOME_MAX_PULSES` too small, or current so low it stalled mid-air, or direction wrong (Phase 1).
- False contact mid-travel → protection too sensitive / current too low; raise current.
- Never stops, no `status=0` → confirm the `0x3E` backstop fires at `HOME_TIMEOUT_MS`; confirm `0x88` protection is actually on.

## Phase 4 — calibrate travel

With homing working: drive to fully-open, read the encoder, and set `OPEN_STEPS` to the real open extent (or push it live via `bush/fire/valve/calibrate`). Then confirm `target` 0.0 / 0.5 / 1.0 land where expected.

## Phase 5 — full Wi-Fi/MQTT + pipeline

1. `circup install adafruit_mcp2515`, copy the real `code.py` (Wi-Fi/MQTT + CAN glue) + `valve.py`, and create `secrets.py` (Wi-Fi + `MQTT_BROKER` = the odroid). The Pico runs two buses: Wi-Fi/MQTT to the odroid + CAN to the motor. No BLE and no `bush_valve_ble` bridge — the Pico subscribes to `bush/fire/valve/*` directly.
2. From MQTT (bush-monitor, `mosquitto_pub`, or `bush-cue`) exercise home / target / breath / nudge; confirm telemetry (`actual`, `status`) comes back.
3. Stream a waveform end-to-end: `bush-cue play sheet.json --no-flame` → odroid MQTT → Pico → CAN → motor; watch `bush/fire/valve/streampos`.

## VERIFY constants — quick reference

| Constant (`valve.py`) | Default | Watch / tune |
|---|---|---|
| `DIR_TOWARD_OPEN/CLOSED` | 0x80 / 0x00 | Phase 1. Wrong = homes the wrong way. |
| `VALVE_CURRENT_MA` | 400 | Seat force on stall-home; raise if it can't move. |
| `MOVE_RPM` / `HOME_RPM` | 40 / 8 | Move speed / gentle approach. |
| `MOVE_ACC` / `BREATH_ACC` / `HOME_ACC` | 2 / 8 / 2 | Ramp smoothness (small = gentle). |
| `HOME_MAX_PULSES` | 6 rev | Must exceed full-open→seat travel. |
| `BREATH_MAX_RPM` | 120 | Breath velocity ceiling. |
| `OPEN_STEPS` | 2000 | Phase 4 recalibration. |
| CAN config (`code.py`) | SCK/MOSI/MISO GP6/GP7/GP4, CS GP5, 500k, 16 MHz, ID 1 | GP4/5 reuse the old UART pins (GP4-7 = SPI0); avoid GP2/3 (relays); wrong crystal = no comms. |

## Open questions to resolve on the bench

- **Limit switch vs stallguard:** the plan homes by stall (the seat is a hard stop). If a physical limit switch gets wired, switch to the 42D native go-home (`0x90` home params + `0x91`) instead — small addition.
- **`0x92` zero semantics:** does set-axis-zero reset the `0x31` readout to 0, or just mark an internal zero? The driver works either way (it re-seeds `_enc_zero_raw` from a post-zero read), but confirm.
- **Stall signalling:** does this 42D firmware emit `0xFD status=0` on a stall, or silently latch (caught only by the `0x3E` backstop)? Determines homing latency.
- **`SR_vFOC` acceptance:** confirm `0x82 05` is honoured by this firmware rev (some ship serial mode locked behind a menu toggle).
- **CAN `crystal_freq`:** confirm the MCP2515 module's crystal (8 vs 16 MHz) and set `CAN_CRYSTAL` in `code.py` to match — a mismatch silently uses the wrong bitrate (no comms).
- **24-bit pulses:** CAN `0xFD` carries a 24-bit pulse count (≤ 16.7 M); confirm `HOME_MAX_PULSES`/`open_steps` stay within that (they do at 16× microstep).

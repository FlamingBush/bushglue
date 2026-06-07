# SERVO42D valve bring-up

Bench plan to validate and tune the **step/dir** rewrite of `valve.py`. The 42C was killed by
reverse power (2026-06-04); the 42D was then run over CAN, but the MCP2515 controller died and CAN
was abandoned (2026-06-06). The valve node now drives the 42D as a **plain stepper over its
STEP/DIR/EN pulse interface** straight from Pico GPIO — no CAN, no UART, no encoder feedback.
Position is dead-reckoned from the step rate (the 42D closes its own loop internally). Reference:
`firmware/valve-control/PROTOCOL.md` (step/dir section).

> Status (2026-06-06): the core path is **VERIFIED on the bench** — boots clean with no motor
> attached, joins Wi-Fi, jogs via step/dir (DIR toward open confirmed), connects to MQTT, and a
> `bush/fire/valve/target` round-trip drives the needle. Still **UNCALIBRATED**: `OPEN_STEPS=2000`
> is a placeholder (Phase 4).

Golden rules for every phase:
- **Check supply polarity before applying power.** Reverse power is what killed the 42C.
- **Set the run current low on the 42D menu** until travel is known — current bounds the force into
  the seat. (Run current is a menu setting now, not a firmware command.)
- E-stop is `bush/fire/valve/stop` over MQTT (or cut power) — via bush-monitor, `mosquitto_pub`, or
  killing `bush-cue play`.

## Before you start

- **Transport is STEP/DIR** — the Pico toggles STEP/DIR GPIO and the 42D closes its own loop to the
  pulses. No controller, no transceiver, no encoder readback. See PROTOCOL.md's step/dir section.
- Wiring: 12–24 V → 42D **`V+`/`GND`** (NOT `IN_*`, those are limit inputs — check polarity, reverse
  power killed the 42C); Pico **`GP4` → 42D `STP`**, **`GP5` → 42D `DIR`**; 42D pulse-port **`COM`
  → 3.3 V**; **common ground** between the Pico and the 42D. **EN is left unwired.**
- Power the 42D, confirm the OLED is on and it can jog from its own menu (proves the servo is alive
  and not bricked).
- **Calibrate the encoder once, motor UNLOADED** (42D menu `Cal`) before attaching the valve — the
  42D's internal closed loop relies on it even though we never read it.
- Confirm 42D menu: **Mode = CR_vFOC** (pulse interface), **MStep = 16** (match `valve.MICROSTEP`),
  **En = Hold** (always-enabled — EN is unwired, so the 42D must ignore the pin), motor type 1.8°,
  **run current low** to start.
- On the Pico: CircuitPython 10.x. No extra libs needed for the motor (step/dir is pure `pwmio`
  + `digitalio`). Pin config is at the top of `code.py`: `PIN_STEP=board.GP4`, `PIN_DIR=board.GP5`,
  `valve.en=None`.

## Phase 0 — step/dir jog test, no MQTT

Prove the pulse path drives the shaft before anything else.

1. Flash `firmware/valve-control/CIRCUITPY/{valve.py,code.py}` + `secrets.py`. (Or for a bare jog,
   from the REPL: set up `valve.step`/`valve.dir` like `code.py` does, `valve.en=None`, then
   `valve.init()` and call `valve._set_velocity(800)` for ~a second, then `valve._set_velocity(0)`.)
2. Expect the console line `Valve(42D step/dir): init -- boot position = 0, ready`. The shaft should
   turn while a non-zero velocity is held and stop at velocity 0.
3. If nothing turns: confirm the 42D menu is **Mode=CR_vFOC** and **En=Hold** (a CR_CAN/serial mode
   or En=pin will ignore the pulses), `COM → 3.3 V` is wired, common ground, and STP/DIR aren't
   swapped.

## Phase 1 — direction sense (before trusting any travel)

`DIR_OPEN_LEVEL=False` means DIR low drives toward OPEN — **verified visually 2026-06-06**, but
re-confirm on your build, motor **unloaded or backed well off the seat**.

1. `nudge +30` (payload is **degrees**; + = toward open in the step/dir driver) and `nudge -30`
   (toward closed). Watch the shaft (30–45° is easier to see than 5°).
2. Confirm `+` physically corresponds to the valve **opening** and `-` to **closing**. If reversed,
   flip `DIR_OPEN_LEVEL` near the top of `valve.py` and repeat.

Note the degree sign convention here is the opposite of the old CAN driver — `valve.py` `_cmd_nudge`
treats `+deg` as toward open.

## Phase 2 — breathing demo (no MQTT)

Validates init + `_set_velocity` + the breath oscillator on the bare motor.

1. Publish a `breath` JSON (or let a `target` enter breathing) and watch the shaft gently oscillate.
2. Tune for a smooth, visible breath: breath `amplitude`, `period_ms`, `skew` (over MQTT), and a
   sane `open_steps` so the velocity isn't rounding to nothing. If the motor doesn't move, amplitude
   × open_steps is too small for a visible step rate — raise one.

## Phase 3 — homing (N/A on this build)

**There is no homing on the step/dir build** — no encoder, no stall/contact sensing. Boot position
= 0 dead-reckoned at wherever the shaft sits, and `home` just re-declares the current position 0.
So there is no "seek the seat" step to validate. Seat safety is procedural now: don't command a
stroke that drives into the force-sensitive seat, and keep `OPEN_STEPS` honest (Phase 4). On the
bench, keep the valve uncoupled or hand-park it mid-travel before powering on. (If a feedback path
or a physical limit switch is ever added, real homing comes back — see
[[project_valve_closed_seat_safety]].)

## Phase 4 — calibrate travel

Hand-park the needle at the closed reference, `home` (declares 0), then jog open to the full-open
extent counting steps (or push a known value live via `bush/fire/valve/calibrate <steps>`). Set
`OPEN_STEPS` to the real open extent. Then confirm `target` 0.0 / 0.5 / 1.0 land where expected.

## Phase 5 — full Wi-Fi/MQTT + pipeline

1. Copy the real `code.py` + `valve.py`, create `secrets.py` (Wi-Fi + `MQTT_BROKER` = the odroid).
   No BLE, no CAN, no `bush_valve_ble` bridge — the Pico subscribes to `bush/fire/valve/*` directly.
2. From MQTT (bush-monitor, `mosquitto_pub`, or `bush-cue`) exercise home / target / breath / nudge;
   confirm telemetry (`actual`, `status`) comes back.
3. Stream a waveform end-to-end: `bush-cue play sheet.json --no-flame` → odroid MQTT → Pico →
   step/dir → motor; watch `bush/fire/valve/streampos`.

## Config — quick reference

| Constant (`valve.py`) | Default | Watch / tune |
|---|---|---|
| `DIR_OPEN_LEVEL` | False | Phase 1. DIR level that opens; flip if a move goes the wrong way. |
| `MICROSTEP` / `STEPS_PER_REV` | 16 / 3200 | Must match the 42D MStep menu. |
| `MAX_SPS` | 24000 | Step-rate ceiling. |
| `MOVE_SPS` | 6400 (~2 rev/s) | Target-move speed. |
| `OPEN_STEPS` | 2000 | **PLACEHOLDER** — Phase 4 calibration. |
| Pins (`code.py`) | STEP=GP4, DIR=GP5, EN=None | GP4/5 reuse the old UART wiring; avoid GP2/3 (relays). |

Run current and the **Mode=CR_vFOC / MStep=16 / En=Hold** menu settings live on the 42D, not in
firmware.

## Open questions to resolve on the bench

- **`OPEN_STEPS` real value:** dead-reckoning means a wrong `OPEN_STEPS` skews every position;
  calibrate it (Phase 4) before trusting `target` fractions.
- **Step loss under load:** with no encoder readback, if the 42D's own loop ever drops steps the
  dead-reckoned position drifts silently. Watch for the needle and `actual` diverging over a long
  run; if it happens, lower `MOVE_SPS`/`MAX_SPS` or raise the menu run current.
- **Seat safety without homing:** there is no closed reference — confirm the procedural rule (don't
  stroke into the seat, keep `OPEN_STEPS` honest) is enough for your install, or add a limit
  switch / feedback path.

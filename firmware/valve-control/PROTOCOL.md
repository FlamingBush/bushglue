# MKS SERVO42D — STEP/DIR pulse interface (current) + serial protocol (history)

The valve node drives the **MKS SERVO42D** closed-loop stepper as a **plain stepper over its
STEP/DIR/EN pulse interface**, straight from Pico 2 W GPIO. There is **no serial/CAN link** to
the motor — the Pico sends step pulses and the 42D closes its own loop internally. The serial
(RS485/UART/CAN) protocol below is **kept for history only** (a transport could return); none of
those bytes are on the wire now.

Transport history: 42C (RS485/UART, died to reverse power 2026-06-04) → 42D over UART → 42D over
CAN (MCP2515) → **42D over STEP/DIR** (the MCP2515 died, CAN abandoned, 2026-06-06). `git log` has
each firmware.

## STEP/DIR pulse interface (CURRENT build — VERIFIED on the bench 2026-06-06)

The 42D is in **pulse mode (CR_vFOC)** and takes step pulses on its STP/DIR pulse port. The Pico
toggles those pins from `valve.py`; no controller, no transceiver, no encoder readback.

- **Velocity primitive:** `_set_velocity(signed_steps_per_sec)` is the only motion call (breath,
  bush-cue stream, target moves all use it). It drives a `pwmio.PWMOut` on STEP whose
  **frequency = |steps/sec|** (duty `0` = stopped, `1<<15` = 50% = running) and sets DIR by sign.
- **Position is dead-reckoned**, not measured: `_set_velocity` integrates `motor_pos_steps` from the
  held velocity × elapsed time and clamps it to `[0, open_steps]`. There is no encoder to ground
  against — telemetry is only as accurate as the step count.
- **Microstep:** `MICROSTEP=16` → `STEPS_PER_REV=3200` (must match the 42D's MStep menu).
- **Direction:** `DIR_OPEN_LEVEL=False` — DIR low drives toward OPEN (VERIFIED visually). Flip the
  constant if a move goes the wrong way.
- **EN is unwired** (`valve.en=None`): the 42D En menu must be **Hold** (always-enabled) so it
  ignores the pin. The lead screw self-holds, so firmware still "de-energizes when idle" by simply
  stopping pulses (no current cut over an unwired EN).
- **No homing:** with no feedback there is no stall/contact sensing. Boot position = 0 dead-reckoned
  at the current shaft pose; `home` = "zero here" (re-declares the current position 0).
- **Run current** is set on the 42D's own menu, NOT in firmware — the `maxtorque` MQTT topic is a
  no-op now.

**42D menu config (required):** Mode = **CR_vFOC** (pulse interface; was CR_CAN), MStep = **16**,
En = **Hold**.

**Pico wiring:** `GP4` → 42D **STP**, `GP5` → 42D **DIR** (the old UART pins, already wired); 42D
pulse-port **COM → 3.3 V** (signals are 3.3 V single-ended); **common ground** between the Pico and
the driver; power **12–24 V → V+/GND** (NOT `IN_*`, those are limit inputs — check polarity, reverse
power killed the 42C). EN left unwired.

Pin/config block in `code.py`: `PIN_STEP=board.GP4`, `PIN_DIR=board.GP5`, `valve.en=None`.

The MQTT topics + bush-cue stream framing + breath JSON interface (below / in `valve.py`) are
unchanged across the transport switch.

---

# (HISTORY) MKS SERVO42D serial protocol — NOT in use on the step/dir build

Everything from here down documents the serial (RS485/UART) and CAN transports that drove the 42D
before the step/dir rewrite. Kept for reference should a serial transport return; it does **not**
describe how the firmware talks to the motor today.

Source: MKS SERVO42D/57D_RS485 User Manual (Makerbase, `github.com/makerbase-motor/MKS-SERVO42D-57D`)
+ the vendor's Arduino examples. RS485 and the TTL-UART port are the **same byte protocol**
(RS485 is just a transceiver in front of the same UART), so the "RS485" manual is correct
for the 3.3 V TTL wiring. Do **not** use the Modbus manual — those bytes differ.

## CAN transport (HISTORY — the MCP2515 died, CAN abandoned 2026-06-06)

The 42D ran briefly on a **Pico 2 W over CAN**. The Pico has no native CAN, so an **MCP2515** SPI
controller + transceiver was required, driven by the `adafruit_mcp2515` lib. The function codes
below are unchanged — only the framing/CRC/transport differed from RS485:

- **Bus:** 500 kbps, standard 11-bit IDs. Motor CAN ID = `ADDR` (MKS default `0x01`),
  broadcast `0x00`. 120 Ω termination at the bus ends.
- **Frame:** arbitration id = motor CAN ID; data = `[func, params…, CRC]`, where
  **CRC = (CAN_ID + func + params) & 0xFF** — the CAN CRC includes the ID (unlike the RS485
  FA/FB byte sum). A reply returns with the same id and `[func, status/data…, CRC]`.
- **8-byte frame limit:** classic CAN data is ≤ 8 bytes, so position moves used a **24-bit
  pulse count** (3 bytes): `0xFD` = `[func, dir|spd_hi, spd_lo, acc, p2, p1, p0, CRC]` = 8 B.
  (`0xF6` speed mode = `[func, dir|spd_hi, spd_lo, acc, CRC]` = 5 B; `0x31` encoder reply =
  `[0x31, int48(6 B), CRC]`, DLC 8.)
- **Source:** MKS SERVO42D/57D **CAN** User Manual (V1.0.6) + the `ricardodeazambuja/mks_servo_can`
  reference.

## Physical layer

| Parameter | Value |
|---|---|
| Baud | **38400** (42D default — no menu change needed) |
| Data/stop/parity | 8N1 |
| Logic level | 3.3 V TTL |
| Slave address | `0x01` (default) |
| XIAO TX | D6 (`board.TX`) → 42D RX |
| XIAO RX | D7 (`board.RX`) ← 42D TX |

## Frame format

```
downlink (host→servo):  FA <addr> <func> <data...> <crc>
uplink   (servo→host):  FB <addr> <func> <data...> <crc>
```

- Head: **`0xFA`** outbound, **`0xFB`** inbound (not an echo of FA).
- `crc = sum(all preceding bytes) & 0xFF` (head + addr + func + data).
- Multi-byte fields are **big-endian**.
- No length field — reply length is per-`func` (the firmware keys a `_RESP_PLEN` table off it).
- Most replies are a 1-byte `status` → 5-byte frame `FB 01 <func> <status> <crc>`.

Worked example: read encoder `FA 01 31 <crc>` → crc = (0xFA+0x01+0x31)&0xFF = `0x2C` → `FA 01 31 2C`.

## Work mode — the gotcha

The 42D ships in **CR_vFOC (pulse mode)** and silently ignores serial motion commands
until switched to a serial mode. `init()` sets **SR_vFOC** (full closed-loop FOC serial):

```
Set work mode SR_vFOC:  FA 01 82 05 <crc>
```

Modes (`0x82` data): 0 CR_OPEN, 1 CR_CLOSE, 2 CR_vFOC (default, pulse), 3 SR_OPEN,
4 SR_CLOSE, **5 SR_vFOC** (serial, closed-loop — our analog to the 42C's CR_UART).

## Commands used by the firmware

| Func | Name | Request data | Reply | Notes |
|---|---|---|---|---|
| `0x31` | Read encoder (addition) | — | `int48` big-endian (6 B) | **Absolute multi-turn ground truth.** +0x4000 (16384) per CW turn → 16384 cts/rev. `_encoder_pos_steps()` scales: 16384/(200·µstep) = 5.12 cts/µstep at 16×. |
| `0x82` | Set work mode | `[mode]` | status | init: `05` (SR_vFOC) |
| `0x83` | Set working current | `[mA hi][mA lo]` uint16 | status | **raw mA, 0–3000** (not 200 mA gears). `maxtorque` topic re-targets this. |
| `0x84` | Set microstep | `[1–255, 0=256]` | status | init: `10` (16×) |
| `0x88` | Locked-rotor protection | `[1 on / 0 off]` | status | init: on — the stall latch homing relies on |
| `0x8C` | Set respond/active | `[respon][active]` | status | init: `01 01` → two-stage motion replies |
| `0xF3` | Enable / disable | `[1 / 0]` | status | de-energized at idle (valve self-holds) |
| `0xF6` | Speed mode | `[dir\|spd_hi][spd_lo][acc]` | status | continuous rotation; **speed 0 stops**. Drives the breath oscillator. |
| `0xFD` | Relative position | `[dir\|spd_hi][spd_lo][acc][pulse int32 BE]` | 2-stage status | moves + nudge + homing seek |
| `0xF7` | Emergency stop | — | status | |
| `0x3D` | Release locked-rotor latch | — | status | clears a stall before re-driving |
| `0x3E` | Read shaft-protection state | — | `[1 latched / 0]` | homing-timeout backstop |
| `0x92` | Set current axis = zero | — | status | homing finalize |

### Motion command encoding (0xF6 / 0xFD)

```
byte4 = (dir<<7) | (speed >> 8) & 0x0F     # dir in b7, speed bits 11..8
byte5 =  speed & 0xFF                        # speed bits 7..0  (12-bit, 0–3000)
acc   = 0–255                                # 0 = instant (no ramp); each unit-RPM
                                             # step takes (256-acc)*50µs, so small
                                             # acc = gentle ramp, large = fast ramp
0xFD only: pulse = uint32 big-endian          # microsteps (3200 = 1 rev at 16×)
```

`dir`: manual says 0 = CCW, 1 = CW. Which physical sense opens vs closes the valve is
**unverified on this build** — `valve.py` `DIR_TOWARD_OPEN/CLOSED` are guesses; swap if a
move drives the wrong way. **Speed is RPM** at microstep ≥ 16 (`actual_rpm = speed·16/µstep`).

### Motion replies — the state machine

With respond=1/active=1 (set in init), a `0xFD` move emits **two** `FB 01 FD <status>` frames:

| status | meaning |
|---|---|
| 1 | run starting (immediate) |
| 2 | run complete |
| 3 | stopped on a limit switch |
| **0** | **fail / stall** |

The firmware drives a state machine off these (`_on_move_reply`): `1` → arm completion
timeout, `2` → re-read the encoder to ground position, `0` → stall. A `0xF6` *run* returns
only `1`/`0`; a `0xF6` stop (speed 0) returns `0`/`1`/`2`.

## Homing — stallguard, no inchworm

The 42D has native locked-rotor (stallguard) detection, so the 42C's hand-rolled
encoder-delta inchworm is gone. `cmd_home()`:

1. Release the latch (`0x3D`), enable, low working current (bounds seat force).
2. Seed the encoder (`0x31`).
3. One `0xFD` toward the closed seat for `HOME_MAX_PULSES` at low RPM.
4. **Contact = the 42D stalls** → `0xFD` returns `status=0` (or, if it latches silently,
   the move times out and a `0x3E` read returns 1). Either path → `_home_on_contact`.
5. Back off `HOME_BACKOFF_STEPS` toward open, verify it moved freely, `0x92` zero there,
   de-energize, re-read `0x31` to seed `_enc_zero_raw`.

So `motor_pos 0` is a margin off the force-sensitive seat, never resting on it — same
safety invariant as before, but the seat is *found* by the controller's stall sensing.

## Differences from the 42C (what broke in the port)

1. Frame is `FA <addr> <func> … <crc>`; replies headed **`FB`** (not `E0`-echo).
2. Must set **SR_vFOC** (`0x82 05`) or serial motion is ignored.
3. **Current is raw mA** (`0x83` uint16, ≤3000), not 200 mA gears.
4. `0xF6`/`0xFD` carry a **12-bit RPM + an accel byte**; `0xFD` pulses are a 32-bit BE field.
5. Encoder via **`0x31` int48** (16384 cts/rev), vs the 42C's `0x30` carry+value (65536).
6. **No `0xA5` max-torque / `0xA4` global accel** — accel is per-command; force is bounded by current.
7. Homing uses native **stallguard**, not the inchworm; zero/home opcodes reorganized
   (`0x92` set-zero, `0x91` go-home, `0x90` home-switch params).
8. Default baud **38400** (42C needed a menu change to 115200).

## Position & breath conventions (current — step/dir)

```
motor_pos 0          = dead-reckoned zero at boot/`home` (no seat reference without feedback)
motor_pos open_steps = fully open (open_steps = OPEN_STEPS, calibrate via bush/fire/valve/calibrate)
MQTT 0.0 = closed, 1.0 = open;  step_position = target * open_steps
```

`motor_pos_steps` is dead-reckoned by `_set_velocity` (no encoder). Breath velocity is computed in
steps/s and fed straight to the STEP PWM frequency; there is no valley re-grounding because there is
nothing to re-ground against. `OPEN_STEPS=2000` is a PLACEHOLDER until travel is calibrated.

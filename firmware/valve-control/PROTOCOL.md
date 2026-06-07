# MKS SERVO42D UART Protocol

Binary serial protocol between the XIAO nRF52840 and the **MKS SERVO42D** closed-loop
stepper servo driving the needle valve. The 42D replaced the 42C (killed by reverse
power, 2026-06-04) and its serial protocol is **substantially different** — this doc
documents the 42D, not the 42C (`git log` for the old 42C reference).

Source: MKS SERVO42D/57D_RS485 User Manual (Makerbase, `github.com/makerbase-motor/MKS-SERVO42D-57D`)
+ the vendor's Arduino examples. RS485 and the TTL-UART port are the **same byte protocol**
(RS485 is just a transceiver in front of the same UART), so the "RS485" manual is correct
for our 3.3 V TTL wiring. Do **not** use the CAN or Modbus manuals — those bytes differ.

> ⚠️ Nothing here has run on a 42D yet. Values tagged VERIFY in `valve.py` (direction
> sense, current, RPM, accel, stall behaviour, 0x92 zero semantics) are estimates and
> must be confirmed on the bench.

## CAN transport (CURRENT build — the hardware is the SERVO42D_CAN variant)

The valve node now runs on a **Pico 2 W over CAN** (the nRF52 is gone). The Pico has no
native CAN, so an **MCP2515** SPI controller + transceiver is required, driven by the
`adafruit_mcp2515` lib (`circup install adafruit_mcp2515`). The function codes below are
unchanged — only the framing/CRC/transport differ from RS485:

- **Bus:** 500 kbps, standard 11-bit IDs. Motor CAN ID = `ADDR` (MKS default `0x01`),
  broadcast `0x00`. 120 Ω termination at the bus ends.
- **Frame:** arbitration id = motor CAN ID; data = `[func, params…, CRC]`, where
  **CRC = (CAN_ID + func + params) & 0xFF** — the CAN CRC includes the ID (unlike the RS485
  FA/FB byte sum). A reply returns with the same id and `[func, status/data…, CRC]`.
- **8-byte frame limit:** classic CAN data is ≤ 8 bytes, so position moves use a **24-bit
  pulse count** (3 bytes): `0xFD` = `[func, dir|spd_hi, spd_lo, acc, p2, p1, p0, CRC]` = 8 B.
  (`0xF6` speed mode = `[func, dir|spd_hi, spd_lo, acc, CRC]` = 5 B; `0x31` encoder reply =
  `[0x31, int48(6 B), CRC]`, DLC 8.)
- **Source:** MKS SERVO42D/57D **CAN** User Manual (V1.0.6) + the `ricardodeazambuja/mks_servo_can`
  reference. Use the CAN manual (not RS485) for framing.

The function-code table below (written for RS485) still applies byte-for-byte as the CAN
**data payload** — only the framing + CRC + transport (MCP2515, not UART) change.

**Pico wiring:** 12–24 V → SERVO42D `V+`/`GND` (NOT `IN_*`, those are limit inputs);
`CAN_H`/`CAN_L` → the MCP2515 transceiver; **common ground** between the Pico/MCP2515 and
the driver. Set the SPI pins / CS / `crystal_freq` (16 MHz Adafruit/Waveshare, 8 MHz generic
module) / motor CAN ID at the top of `code.py`.

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

## Position & breath conventions (unchanged)

```
motor_pos 0          = closed seat margin (zero set at homing)
motor_pos open_steps = fully open
MQTT 0.0 = closed, 1.0 = open;  step_position = target * open_steps
```

Position is read from `0x31` after every move/nudge and once per breath cycle at the
valley (the bottom rest, where the motor is briefly stopped — the only reliable read
point), bounding breath drift to a single cycle. Breath velocity is computed in steps/s,
converted to RPM for the `0xF6` speed field.

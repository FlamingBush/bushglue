# MKS SERVO42D — CAN bus protocol (current)

The valve node drives the **MKS SERVO42D** closed-loop stepper servo (needle valve) over
**CAN**, via an **MCP2515** SPI-CAN controller and `valve.py`. CAN is the only motor link now:
UART/RS485 and a brief STEP/DIR pulse-interface interlude are retired (kept as history below).

The node is a small fleet, all running the same `valve.py` CAN driver and differing only in
**host link** + **CAN carrier**: **Pico 2 W + CanBerry** (Wi-Fi → MQTT, `code.py`), **XIAO
nRF52840 + MCP2515** (BLE → `bush_valve_ble` bridge, `code_xiao_ble.py`), and **Waveshare
RP2350-CAN** (onboard XL2515, no radio → USB-serial → `bush_valve_serial` bridge,
`code_usb_serial.py`) are the nodes. A reflashed **BridgePlate + CanBerry** centerpiece, also
over USB-serial, is still planned (no hardware yet).

Transport history (`git log` has each firmware): 42C RS485/UART (died to reverse power
2026-06-04) → 42D over UART → 42D over CAN (MCP2515) → a STEP/DIR pulse interlude (`fe19073`,
after the first MCP2515 died) → **back to 42D over CAN** with new CAN silicon. Recover the
step/dir firmware with `git show 3f40ee8:firmware/valve-control/CIRCUITPY/code.py` (+ `valve.py`);
the UART firmware with `git show 9c709f5^:firmware/valve-control/CIRCUITPY/code_xiao_ble.py`.

Source for the function codes: MKS SERVO42D/57D **CAN** User Manual (V1.0.6) +
`ricardodeazambuja/mks_servo_can`. The RS485 manual's function-code table applies byte-for-byte
as the CAN data payload — only framing/CRC/transport differ (see below). Do **not** use the
Modbus manual — those bytes differ.

> ✅ **CAN closed loop is bench-proven (2026-06-10, Waveshare RP2350-CAN).** Verified +
> locked in: `DIR_TOWARD_OPEN=0x00` / `DIR_TOWARD_CLOSED=0x80` (encoder counts up closing,
> `_enc_sign=-1`); current = torque (`0x83`, the only knob); lockrotor protection kept ON as
> the seat detector (`0x3E`) + jam net; `OPEN_STEPS=11200` (full travel ~3.76 rev). Homing is
> ENABLED and GENTLE (`HOMING_DISABLED=False`) — see *Homing* below. Bus bring-up needs a 120 Ω
> terminator or the controller can't even transmit (TXREQ stuck, TEC frozen at 0). RPM/accel
> envelope is still untuned (the operator pushes those live).

## CAN transport (CURRENT)

The MCU has no native CAN, so every node uses an **MCP2515** SPI-CAN controller + transceiver,
driven by the `adafruit_mcp2515` lib (`circup install adafruit_mcp2515`; it **polls** — no INT
pin needed). The board glue (`code.py` / `code_xiao_ble.py`) builds the MCP2515 and assigns
`valve.can` + `valve.Message` before `valve.init()`; `valve.py` is carrier-agnostic. The
function codes are unchanged from RS485 — only the framing/CRC/transport differ:

- **Bus:** 500 kbps, standard 11-bit IDs. Motor CAN ID = `ADDR` (MKS default `0x01`),
  broadcast `0x00`. 120 Ω termination at the bus ends.
- **Frame:** arbitration id = motor CAN ID; data = `[func, params…, CRC]`, where
  **CRC = (CAN_ID + func + params) & 0xFF** — the CAN CRC includes the ID (unlike the RS485
  FA/FB byte sum). A reply returns with the same id and `[func, status/data…, CRC]`.
- **8-byte frame limit:** classic CAN data is ≤ 8 bytes, so position moves use a **24-bit
  pulse count** (3 bytes): `0xFD` = `[func, dir|spd_hi, spd_lo, acc, p2, p1, p0, CRC]` = 8 B.
  (`0xF6` speed mode = `[func, dir|spd_hi, spd_lo, acc, CRC]` = 5 B; `0x31` encoder reply =
  `[0x31, int48(6 B), CRC]`, DLC 8.)

The function-code table further down (written for RS485) still applies byte-for-byte as the CAN
**data payload** — only the framing + CRC + transport (MCP2515, not UART) change.

### CAN carriers (which SPI pins + crystal)
`code.py` selects a profile from `secrets["BOARD"]`; `code_usb_serial.py` from `settings.toml`
`BOARD=` (default `waveshare`). **`crystal_freq` MUST match the module's crystal or the bus
silently never ACKs** — this is the #1 bring-up failure.

| `BOARD` | Controller / transceiver | SPI clk/mosi/miso/cs | Crystal | Notes |
|---|---|---|---|---|
| `canberry` | MCP2515 + MCP2551 | Pico 2 W `GP6/GP7/GP4/GP5` | **16 MHz** | CanBerry Pi V1.1.1 (IndustrialBerry, open HW). MCP2551 needs **5 V** (from the Pico `VBUS`). |
| `waveshare` | XL2515 + SIT65HVD230 (3.3 V) | `GP10/GP11/GP12/GP9` | **16 MHz** | Waveshare RP2350-CAN (SPI1, INT `GP8`); schematic-verified, CAN bring-up bench-confirmed. No radio → USB-serial node. 3.3 V transceiver — no 5 V rail. |
| XIAO | MCP2515 module | `board.SCK/MOSI/MISO` + `D3` cs | 16 MHz typical | `code_xiao_ble.py`; the CS pin is a wiring choice. |

**Common to every carrier:** 12–24 V → SERVO42D `V+`/`GND` (NOT `IN_*`, those are limit inputs —
check polarity, reverse power killed the 42C); `CAN_H`/`CAN_L` → the transceiver; **common
ground**; 120 Ω at both bus ends. **42D menu: CAN rate 500 kbps, CAN ID 1, MStep 16.** The
firmware sets work mode **SR_vFOC** and the run current over the bus in `valve.init()` (there is
no "CR_CAN" work mode — see *Work mode* below; enable/disable is the `0xF3` command, no EN pin).

> **CanBerry on a Pi-Plates stack** (the future BridgePlate centerpiece) adds coexistence rules
> — lift the MCP2515 INT off **BCM25** (we poll), MCP2515 CS = **CE0**, JP2 off / JP3 on. Not
> relevant to the Pico-2-W-direct wiring above; see the implementation plan for the stack build.

### Host links (MCU → MQTT)
The motor link is always CAN; the **host link** (how a node reaches the `bush/fire/valve/*` MQTT
topics) differs per node. All three speak the same newline-framed `"<topic> <payload>"` line
protocol — `valve.py`'s `(topic, payload)` interface IS that protocol, so the bridges do no
translation.

| Node | Host link | Firmware | Host bridge |
|---|---|---|---|
| Pico 2 W | Wi-Fi → MQTT direct | `code.py` | none (MQTT in firmware) |
| XIAO nRF52840 | BLE Nordic-UART | `code_xiao_ble.py` | `bush_valve_ble` (bleak) |
| Waveshare / BridgePlate | USB-serial (CDC) | `code_usb_serial.py` | `bush_valve_serial` (pyserial) |

**USB-serial specifics:** `boot.py` runs `usb_cdc.enable(console=True, data=True)` — REPL on
`console`, line protocol on the clean `data` CDC. boot.py only takes effect on a **hard reset**
(power-cycle / `microcontroller.reset()`), not a soft reload. The board then enumerates **two**
serial ports sharing one USB serial number; `bush_valve_serial` auto-picks the **data** CDC (the
higher interface), overridable with `BUSH_VALVE_SERIAL_PORT` (point it at a stable
`/dev/serial/by-id/...-if02` on the odroid). Binary `0xF5` stream frames pass straight through;
both sides keep I/O non-blocking so `valve.service()` deadlines hold.

## RS485 / UART framing (HISTORY — superseded by CAN above)

The 42D's UART/RS485 transport is retired (recover with
`git show 9c709f5^:firmware/valve-control/CIRCUITPY/code_xiao_ble.py`). Under CAN the framing is
different (see *CAN transport* above), **but the function codes, the per-`func` reply-length
table, and the big-endian field convention are transport-agnostic** and describe the current CAN
data payload too. Only the FA/FB head + the byte-sum CRC + the UART physical layer are history.

### Physical layer (UART)

| Parameter | Value |
|---|---|
| Baud | **38400** (42D default) |
| Data/stop/parity | 8N1 |
| Logic level | 3.3 V TTL |
| Slave address | `0x01` (default) |
| XIAO TX | D6 (`board.TX`) → 42D RX |
| XIAO RX | D7 (`board.RX`) ← 42D TX |

### Frame format (UART)

```
downlink (host→servo):  FA <addr> <func> <data...> <crc>
uplink   (servo→host):  FB <addr> <func> <data...> <crc>
```

- Head: **`0xFA`** outbound, **`0xFB`** inbound (not an echo of FA).
- `crc = sum(all preceding bytes) & 0xFF` (head + addr + func + data). *(CAN instead uses
  `(CAN_ID + func + params) & 0xFF` — see above.)*
- Multi-byte fields are **big-endian** (CAN too).
- No length field — reply length is per-`func` (the firmware keys a `_RESP_PLEN` table off it;
  used on both transports).
- Most replies are a 1-byte `status` → 5-byte frame `FB 01 <func> <status> <crc>`.

Worked example (UART): read encoder `FA 01 31 <crc>` → crc = (0xFA+0x01+0x31)&0xFF = `0x2C` →
`FA 01 31 2C`. The CAN equivalent is data `[0x31, crc]` with `crc = (0x01+0x31)&0xFF = 0x32`.

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

`dir`: manual says 0 = CCW, 1 = CW. **Bench-verified 2026-06-10:** `DIR_TOWARD_OPEN=0x00`
(encoder counts DOWN), `DIR_TOWARD_CLOSED=0x80` (counts UP), `_enc_sign=-1`. **Speed is RPM**
at microstep ≥ 16 (`actual_rpm = speed·16/µstep`).

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

## Homing — gentle protection-seek (ENABLED, bench-proven 2026-06-10)

`HOMING_DISABLED=False`. `cmd_home()` is **blocking** and **deliberately gentle**: a healthy
needle valve is smooth across its whole travel, so a low seek current traverses it freely and
only stalls at the seat. It does **NOT** bump current to force past resistance — forcing shreds
the internals (that grinding is what creates sticky spots), so rough homing perpetuates damage.

1. Protection ON (`0x88 01`), release any latch (`0x3D`), enable, set `HOME_SEEK_CUR` (300 mA — gentle).
2. One `0xFD` toward the closed seat (`DIR_TOWARD_CLOSED=0x80`) for `HOME_MAX_PULSES` at `HOME_RPM`.
3. **Seat = the 42D's locked-rotor latch fires**, read via `0x3E`==1 (polled with `_blocking_read_status`).
4. Back off `HOME_BACKOFF_STEPS` toward open, verify it moved (else error `home_stuck` — a worn
   valve that won't move gently is reported, never pushed), set `_enc_zero_raw`/`_enc_sign=-1`.

So `motor_pos 0` is a margin off the seat, never resting on it. A worn valve errors instead of
being forced. Protection stays ON afterward as the operating jam net. **The 42D's own internal
homing** (`0x90` go-home params + `0x91` GoHome + `0x92` set-zero, monitored via `0xF1`/`0x3E`)
is the next avenue — configure it equally gentle, and mind the NVM auto-home-rams-on-boot gotcha
(`init` sends SET_ZERO_MODE `0x00`). The encoder-flatline ramp-to-stall "method b" is in git.

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

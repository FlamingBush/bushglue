# MKS SERVO42C-MT V1.1 UART Protocol

Binary serial protocol between the Pico 2 W and the MKS SERVO42C-MT V1.1 integrated closed-loop stepper servo.

Consolidated from the upstream wiki (https://github.com/makerbase-mks/MKS-SERVO42C/wiki) and our own field experience. Covers every documented command, the on-board menu, and what's specific to the **MT** (multi-turn) variant we use. The wiki itself documents only the base SERVO42C — MT-specific behavior is called out inline.

## Hardware

| Spec | Value |
|---|---|
| Operating voltage | 7–28 V |
| Current range | 0–3000 mA (200 mA gear steps via 0x83) |
| Max speed | 1000 RPM |
| Angle resolution | 0.08° |
| Display | OLED + 4-button menu |
| Driver | 4× half-bridge, 8× MOSFET |
| Encoder | Closed-loop magnetic (multi-turn on **MT** variant) |

## Physical Layer

| Parameter | Value |
|---|---|
| Baud rate | 115200 (must be set in MKS on-board menu: `UartBaud` = 6; factory default is 38400) |
| Data bits | 8 |
| Stop bits | 1 |
| Parity | None |
| Logic level | 3.3V TTL |
| Pico TX pin | GP4 |
| Pico RX pin | GP5 |

The MKS UART must be explicitly enabled via the on-board menu before serial commands will work. Set `UartBaud` to your desired rate.

## Packet Format

All communication is binary. Every packet follows:

```
[ADDR] [CMD] [PARAM_0] ... [PARAM_N] [CHK]
```

| Field | Size | Description |
|---|---|---|
| ADDR | 1 byte | Device address. Default `0xE0`. Configurable `0xE0`-`0xE9`. |
| CMD | 1 byte | Command code |
| PARAM | 0-5 bytes | Parameters (count depends on command) |
| CHK | 1 byte | Checksum: `(ADDR + CMD + PARAM_0 + ... + PARAM_N) & 0xFF` |

Responses use the same format. Multi-byte values are **big-endian**.

Standard success/fail response: `[ADDR] [0x01] [CHK]` = success, `[ADDR] [0x00] [CHK]` = fail.

All parameters are mandatory. Do not omit a parameter byte even if its value is zero — the controller treats short packets as errors.

## Initialization Sequence

The Pico sends these on boot (see `valve.py:init()`):

1. Set work mode to CR_UART: `E0 82 02 64`
2. Set microstepping to 16: `E0 84 10 74`
3. Set current to 200mA (gear 1): `E0 83 01 64`
4. Enable stall protection: `E0 88 01 69`
5. Enable motor: `E0 F3 01 D4`

The motor must be in CR_UART mode for serial motion commands to work.

## Command Index

| Opcode | Direction | Name | Used by firmware |
|---|---|---|---|
| 0x30 | Read | Encoder value (multi-turn on MT) | Yes — homing stall detection |
| 0x33 | Read | Pulse count (commanded steps) | No |
| 0x36 | Read | Motor angle | No (layout varies between FW revs) |
| 0x39 | Read | Angle error (desired − actual) | No — useful diagnostic |
| 0x3A | Read | Enable-pin status | No — useful diagnostic |
| 0x3D | Write | Clear stall-protect latch | Yes — at start of homing |
| 0x3E | Read | Motor shaft status (blocked?) | No — useful diagnostic |
| 0x3F | Write | Restore factory defaults | No — recovery (resets baud) |
| 0x80 | Write | Calibrate encoder | No — run once unloaded |
| 0x81 | Write | Set motor type | No (set via menu) |
| 0x82 | Write | Set control mode | Yes (init: CR_UART) |
| 0x83 | Write | Set current gear | Yes (init: 200 mA) |
| 0x84 | Write | Set microstepping | Yes (init: 16×) |
| 0x85 | Write | Set EN-pin active level | No |
| 0x86 | Write | Set rotation direction | No |
| 0x87 | Write | Auto-shutdown screen | No |
| 0x88 | Write | Set stall protection | Yes (init: on) |
| 0x89 | Write | Subdivision interpolation | No |
| 0x8A | Write | Set baud rate | No (set via menu) |
| 0x8B | Write | Set UART address | No |
| 0x90 | Write | Set zero mode | Yes (homing) |
| 0x91 | Write | Set current position as zero | No — handled by 0x94 |
| 0x92 | Write | Set zero speed | Yes (homing) |
| 0x93 | Write | Set zero direction | Yes (homing) |
| 0x94 | Write | Return to zero (home) | Yes |
| 0xA1–A5 | Write | Position PID / accel / torque | No (defaults work) |
| 0xF3 | Write | Enable/disable motor (UART) | Yes (init: enable) |
| 0xF6 | Write | Constant-speed move | No |
| 0xF7 | Write | Emergency stop | Yes |
| 0xFD | Write | Relative move (position) | Yes — primary motion |
| 0xFF | Write | Save / clear status (C8 = save, CA = clear) | No |

## Commands (detail)

### Read Encoder (0x30)

```
TX: E0 30 10
RX: E0 [int32 carry] [uint16 value] CHK    (8 bytes total)
```

The base SERVO42C wiki says this returns a 16-bit value (0–FFFF). **The MT variant we use extends it to 48 bits**: 4-byte signed `carry` (turn count) + 2-byte unsigned `value` (within-rotation position). Combined raw position: `(carry << 16) | value`. The firmware does not interpret either field on its own — it only compares raw values across reads to detect movement.

Used for homing-stall detection: a constant raw value for `HOME_STALL_MS` (3 s) is treated as "motor reached its physical stop".

V1.1.2 firmware also responds to `0x36`, but the byte layout is undocumented and varies between firmware revisions. Stick to `0x30` on this hardware.

### Read Pulse Count (0x33)

```
TX: E0 33 13
RX: E0 [int32 pulses] CHK    (6 bytes total)
```

Returns the controller's accumulated commanded pulse count. Diagnostic only — the closed-loop position is `0x30`'s encoder reading. Useful for detecting "the controller thinks it moved but the encoder disagrees" failure modes.

### Read Angle Error (0x39)

```
TX: E0 39 19
RX: E0 [int16 error] CHK    (4 bytes total)
```

Difference between desired and actual angle. Diagnostic.

### Read Enable Pin Status (0x3A)

```
TX: E0 3A 1A
RX: E0 [status] CHK
```

Status: `0x01` = enabled, `0x02` = disabled, `0x00` = error.

### Read Motor Shaft Status (0x3E)

```
TX: E0 3E 1E
RX: E0 [status] CHK
```

Status: `0x01` = stalled/blocked, `0x02` = running normally, `0x00` = error.

### Clear Stall-Protect Latch (0x3D)

```
TX: E0 3D 1D
RX: E0 [result] CHK
```

Clears the latched stall-protection trigger so a subsequent move can run. Not in the upstream wiki — discovered in firmware reverse-engineering. Our `cmd_home()` sends this first so a stalled-and-aborted prior move doesn't block the homing chain.

### Restore Factory Defaults (0x3F)

```
TX: E0 3F 1F
RX: E0 [result] CHK
```

**Resets baud rate too.** After this command, the controller falls back to its factory default (38400) and you must reconfigure `UartBaud` via the on-board menu before serial works again. Use only when the controller is in a known-bad state that survives power cycles.

### Enable/Disable Motor (0xF3)

```
Enable:  E0 F3 01 D4
Disable: E0 F3 00 D3
```

### Emergency Stop (0xF7)

```
TX: E0 F7 D7
RX: E0 01 E1
```

### Move to Position (0xFD)

Primary motion command. Moves a relative number of pulses at a given speed.

```
TX: E0 FD [speed_dir] [pulse_B3] [pulse_B2] [pulse_B1] [pulse_B0] CHK
RX: E0 01 E1    (status=1, "starting", sent immediately)
RX: E0 02 E2    (status=2, "complete", sent when motion finishes)
RX: E0 00 E0    (status=0, "stalled/rejected", on locked-rotor or error)
```

The two-stage response is critical: a 0xFD command emits **two** 3-byte ACKs on success. Parsers that expect a single ACK will misalign on the second one. On stall, a single status=0 is emitted in place of the status=2.

The motor returns status=2 even when it didn't physically move (e.g. when the work mode isn't CR_UART). Because of this, init failure modes can be silent — diagnose by reading the encoder before and after a test move if you suspect this.

**Speed/direction byte encoding:**
- Bit 7: direction (0 = CW, 1 = CCW)
- Bits 6-0: speed gear (1-127)

In our valve convention:
- CW (bit 7 = 0) = toward closed (decreasing position)
- CCW (bit 7 = 1) = toward open (increasing position)

**Pulse count:** 4-byte big-endian unsigned integer. Number of microstep pulses to move.

Example — move CCW (toward open) 3200 pulses (1 revolution) at speed 10:
```
TX: E0 FD 8A 00 00 0C 80 CHK
     │    │  │  └──────────┘ pulse count = 3200 = 0x00000C80
     │    │  └─ speed 10 | direction CCW (0x80 | 0x0A = 0x8A)
     │    └─ CMD_MOVE_POS
     └─ address
```

### Constant Speed (0xF6)

```
TX: E0 F6 [speed_dir] CHK
```

Continuous rotation at a fixed speed in a fixed direction. Same `speed_dir` byte encoding as 0xFD. Stops on a second 0xF6 with speed=0 or on 0xF7. Not used by our firmware.

### Return to Zero (0x94)

Drives toward the configured zero direction until stall, then sets position as zero.

```
TX: E0 94 00 74
RX: E0 01 E1    (when complete)
```

Before issuing, configure zero behavior:
- Set zero mode to DirMode: `E0 90 01 71`
- Set zero direction to CCW: `E0 93 01 74`
- Set zero speed to slowest: `E0 92 04 76`

The 0x94 response is unreliable at low current — the firmware fires it without expecting an ACK and detects completion via encoder-stall fallback in `service()`.

### Set Current Position as Zero (0x91)

```
TX: E0 91 00 71
RX: E0 01 E1
```

### Set Motor Current (0x83)

```
TX: E0 83 [gear] CHK
```

Gear values: `0x00`=0mA, `0x01`=200mA, `0x02`=400mA, ... `0x0C`=2400mA (200mA steps).

Default for this project: `0x01` (200mA) — stall-as-fuse.

### Set Work Mode (0x82)

```
TX: E0 82 [mode] CHK
```

Modes: `0x00`=CR_OPEN, `0x01`=CR_vFOC, `0x02`=CR_UART.

### Set Microstepping (0x84)

```
TX: E0 84 [subdiv] CHK
```

Subdivision: `0x01`-`0xFF` (1-255), `0x00`=256. Default for this project: `0x10` (16).

### Set Stall Protection (0x88)

```
TX: E0 88 01 69    (enable)
TX: E0 88 00 68    (disable)
```

When enabled, the controller halts and emits `status=0` on a 0xFD instead of indefinitely stalling. Required for our homing-by-stall convention to terminate cleanly.

### Save / Clear Status (0xFF)

```
TX: E0 FF C8 ...   (save: persist current params to NVRAM)
TX: E0 FF CA ...   (clear: discard pending status)
```

Wiki note: "disables after save" — the controller stops accepting new commands until power-cycled or otherwise reset. Use sparingly.

## Recovery

When the controller gets into a bad state:

1. **Soft path:** disable + re-enable motor (`E0 F3 00 D3` then `E0 F3 01 D4`). Clears most transient errors. Encoder reads should resume.
2. **Re-init:** re-run the full init sequence (lines 1–5 above). Safe to do any time.
3. **Restore defaults (0x3F):** resets everything including baud rate. After this you must reconfigure `UartBaud` via the on-board menu before serial works again.
4. **Power cycle:** when all else fails. The MT variant retains absolute encoder position across power cycles, so re-homing isn't strictly required for accuracy — but our firmware doesn't know that and will refuse moves until homed.

If the controller stops responding to UART entirely (encoder reads return nothing, ACKs never arrive): suspect physical-layer trouble first. Check wiring at GP4/GP5, then the MKS power LED, then the OLED display. Conductive alkaline dust shorting the driver is the documented Burning Man failure mode (see `CALIBRATION.md`).

## On-Board Menu Reference

The MKS controller has an OLED + 4-button menu. Below is the complete menu tree.

| Item | Options | What it does |
|---|---|---|
| **CAL** | — | Calibrate the encoder. Run once with motor unloaded. |
| **MotType** | 0.9°, 1.8° | Motor step angle. Set to 1.8° for NEMA 17 |
| **Mode** | CR_OPEN, CR_vFOC, CR_UART | Control mode. Firmware sets CR_UART via 0x82 on boot |
| **Ma** | 0–4095 | Current in CR_OPEN mode only. Ignored under CR_UART |
| **MStep** | 1, 2, 4, 8, 16, 32, 64, 128, 256 | Microstepping. Firmware sets 16 via 0x84 on boot |
| **En** | H, L, Hold | EN-pin active level. Irrelevant under UART control |
| **Dir** | CW, CCW | Positive direction. Our firmware uses raw bit-7 in 0xFD, so this is don't-care |
| **AutoSDD** | Enable, Disable | OLED sleep |
| **Protect** | Enable, Disable | Stall protection. Firmware re-asserts via 0x88 on boot |
| **MPlyer** | Enable, Disable | Internal 256-subdivision interpolation |
| **UartBaud** | Disable, 9600–115200 (1=9600 … 6=115200) | **Must be 6 (115200) for our firmware.** Factory default is 38400 |
| **UartAddr** | 0xE0–0xE9 (0–9) | **Must be 0 (0xE0)** to match firmware |
| **0_Mode** | Disable, DirMode, NearMode | Auto-homing behavior on power-on. Firmware drives homing explicitly, so this can be Disable |
| **Set 0** | — | Sets current physical position as zero |
| **0_Speed** | 0–4 | Auto-home speed. Smaller = faster. Firmware sets via 0x92 |
| **0_Dir** | CW, CCW | Auto-home direction. Firmware sets via 0x93 |
| **Goto 0** | — | Trigger return-to-zero from menu |
| **ACC** | Disable, 286–1042 | Acceleration profile. Wiki warns: too-large values can damage the board |
| **Restore** | — | Reset all parameters to defaults (including baud!). Equivalent to 0x3F |
| **Exit** | — | Leave menu |

The firmware-required settings (must be set by hand once per controller, before any serial use):

- `UartBaud` = 6 (115200)
- `UartAddr` = 0 (0xE0)
- `MotType` = 1 (1.8°)

Everything else is overridden via serial commands on each boot.

## Checksum Calculation

```python
def checksum(packet_bytes):
    return sum(packet_bytes) & 0xFF
```

Example: `E0 36` -> `(0xE0 + 0x36) & 0xFF = 0x16` -> full packet: `E0 36 16`

## Position Convention

```
Step 0           = fully closed (CW limit, needle seated)
Step open_steps  = fully open (CCW limit, homing stop)

MQTT target 0.0  = closed
MQTT target 1.0  = open

step_position = mqtt_target * open_steps
```

The motor homes by driving CCW (toward open) until it stalls against the bonnet thread stop. This is safe because the open stop is a non-sealing mechanical limit. Never home toward the needle seat.

## Speed Reference

At 16x microstepping on a 1.8 deg motor:
```
RPM = (speed_gear * 30000) / (16 * 200)
    = speed_gear * 9.375
```

| Speed Gear | RPM | Use Case |
|---|---|---|
| 1 | 9.4 | Ultra-slow positioning |
| 10 | 93.8 | Homing |
| 20 | 187.5 | Normal valve movement |
| 127 | 1190.6 | Maximum (not recommended for valve) |

## MT Variant Notes

The MT (multi-turn) variant differs from the base SERVO42C in:

- **Absolute multi-turn encoder.** Position is tracked across turns and persists through power cycles. `0x30` returns 48 bits instead of the documented 16.
- **The base wiki documents only the single-turn variant.** Treat any "0–FFFF" range note in the wiki as the within-rotation portion of the MT response. Our `0x30` parser reads 8 bytes total.
- All other opcodes behave identically.

## References

- Upstream wiki (most authoritative): https://github.com/makerbase-mks/MKS-SERVO42C/wiki
- Upstream repo (firmware, hardware, vendor PDFs): https://github.com/makerbase-mks/MKS-SERVO42C
- Vendor PDFs live under `01_Makerbase SERVO42C Related documents/` in the upstream repo. Not mirrored here to keep the bushglue repo small; clone the upstream if you need them.

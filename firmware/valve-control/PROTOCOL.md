# MKS SERVO42C-MT V1.1 UART Protocol

Binary serial protocol between the Pico 2 W and the MKS SERVO42C-MT V1.1 integrated closed-loop stepper servo.

## Physical Layer

| Parameter | Value |
|---|---|
| Baud rate | 115200 (must be set in MKS on-board menu: `UartBaud` = 6) |
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

## Initialization Sequence

The Pico sends these on boot (see `valve.py:init()`):

1. Set work mode to CR_UART: `E0 82 02 64`
2. Set microstepping to 16: `E0 84 10 74`
3. Set current to 200mA (gear 1): `E0 83 01 64`
4. Enable motor: `E0 F3 01 D4`

The motor must be in CR_UART mode for serial motion commands to work.

## Commands Used

### Read Motor Shaft Angle (0x36)

Returns multi-turn accumulating position.

```
TX: E0 36 16
RX: E0 [int32 big-endian] CHK    (6 bytes total)
```

Value: 0-65535 per rotation. Accumulates across multiple turns (signed int32).

Conversion to steps (16x microstepping, 1.8 deg motor):
```
steps_from_zero = (raw_angle * 3200) / 65536
```

### Read Stall Status (0x3E)

```
TX: E0 3E 1E
RX: E0 [status] CHK    (3 bytes total)
```

Status: `0x01` = stalled/blocked, `0x02` = running normally, `0x00` = error.

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
```

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

## References

- [MKS SERVO42C Wiki](https://github.com/makerbase-mks/MKS-SERVO42C/wiki)
- Source: `makerbase-mks/MKS-SERVO42C` on GitHub

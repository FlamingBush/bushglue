# Relay Control Board

## Hardware

| Field | Value |
|---|---|
| Board | Raspberry Pi Pico 2 W |
| Board ID | `raspberry_pi_pico2_w` |
| CircuitPython | 10.0.3 (2025-10-17) |

## Pin Assignments

| Pin | Function |
|---|---|
| GP2 | Flare relay output |
| GP3 | Big jet relay output |
| GP4 | UART TX → MKS SERVO42C RX (needle valve) |
| GP5 | UART RX ← MKS SERVO42C TX (needle valve) |
| GP9 | Poof relay output |

## MQTT Topics

| Topic | Direction | Payload | Cadence / Effect |
|---|---|---|---|
| `bush/flame/pulse` | sub | `{"valve":"flare","ms":350}` | Fire named valve for N ms. Valid valves: `flare`, `bigjet`, `poof` |
| `bush/fire/valve/target` | sub | `0.5` or `{"target":0.5}` | Set needle valve position (0.0=closed, 1.0=open). Rate-limited to 10 Hz in firmware (`TARGET_MIN_MS=100`) |
| `bush/fire/valve/home` | sub | (any) | Initiate homing sequence (drive to open stop) |
| `bush/fire/valve/stop` | sub | (any) | Emergency stop |
| `bush/fire/valve/calibrate` | sub | `16000` or `{"steps":16000}` | Set open_steps calibration (volatile — does not persist) |
| `bush/fire/valve/breath` | sub | JSON: `{"amplitude":0.04, "period_ms":5000, "skew":0.5, "enabled":true}` | Tune the firmware-side breathing oscillator. Partial updates supported (omitted fields unchanged). `skew` < 0.5 = opens faster than closes |
| `bush/fire/valve/actual` | pub | `0.42` | Current fractional position. **Every 250 ms** (`ACTUAL_MS`) |
| `bush/fire/valve/status` | pub | JSON: `{state, pos, target, homed, stalled, last_error}` | **Every 1000 ms idle / 200 ms moving** (`STATUS_IDLE_MS` / `STATUS_MOVE_MS`) |
| `bush/fire/valve/online` | pub | `online` / `offline` | Retained. Published `online` on MQTT connect; broker LWT delivers `offline` on disconnect |

`pos` in the status JSON is the same fractional position published on `actual`; both are derived from the firmware's `motor_pos_steps` (commanded step count, clamped to `[0, open_steps]`), **not** from a live encoder read. The MKS encoder is only polled during homing-stall detection (see `PROTOCOL.md` — `0x30 Read Encoder`). If you need true measured position vs. commanded, query 0x30 directly over UART.

## Required CircuitPython Libraries

Install from the [CircuitPython Library Bundle](https://circuitpython.org/libraries)
matching CircuitPython 10.x:

- `adafruit_minimqtt` (folder)
- `adafruit_requests.mpy`
- `adafruit_connection_manager.mpy`
- `adafruit_ticks.mpy`

Copy the above into `CIRCUITPY/lib/`.

TODO bundle these

## Firmware Files (CIRCUITPY root)

| File | Purpose |
|---|---|
| `code.py` | **Active firmware** — non-blocking MQTT GPIO pulse controller + valve integration |
| `valve.py` | Motorized needle valve control via MKS SERVO42C-MT V1.1 UART |
| `secrets.py` | WiFi + MQTT credentials (copy from `secrets.example.py`, do not commit) |

## Rebuild Steps

1. Flash CircuitPython 10.x onto the Pico 2 W.
2. Copy all files from `CIRCUITPY/` in this package to the `CIRCUITPY` drive.
3. Copy `secrets.example.py` to `secrets.py` and fill in credentials.
4. Install libraries listed above into `CIRCUITPY/lib/`.
5. The board auto-starts `code.py` on power-up.

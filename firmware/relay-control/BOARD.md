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

| Topic | Payload | Effect |
|---|---|---|
| `bush/flame/pulse` | `{"valve":"flare","ms":350}` | Fire named valve for N ms. Valid valves: `flare`, `bigjet`, `poof` |
| `bush/fire/valve/target` | `0.5` or `{"target":0.5}` | Set needle valve position (0.0=closed, 1.0=open) |
| `bush/fire/valve/home` | (any) | Initiate homing sequence (drive to open stop) |
| `bush/fire/valve/stop` | (any) | Emergency stop |
| `bush/fire/valve/calibrate` | `16000` or `{"steps":16000}` | Set open_steps calibration |
| `bush/fire/valve/actual` | `0.42` | (published) Current fractional position |
| `bush/fire/valve/status` | JSON | (published) State, position, errors |
| `bush/fire/valve/online` | `online`/`offline` | (published) Availability |

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

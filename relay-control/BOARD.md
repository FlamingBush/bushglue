# Relay Control Board

## Hardware

| Field | Value |
|---|---|
| Board | Raspberry Pi Pico 2 W |
| Board ID | `raspberry_pi_pico2_w` |
| CircuitPython | 10.0.3 (2025-10-17) |
| UID | `77FD8A1B8F89874F` |
| MAC | `88:A2:9E:45:94:A3` |

## Pin Assignments

| Pin | Function |
|---|---|
| GP2 | Flare relay output |
| GP3 | Big jet relay output |

## MQTT Topics

| Topic | Payload | Effect |
|---|---|---|
| `bush/flame/flare/pulse` | integer ms | Fire flare for N ms |
| `bush/flame/bigjet/pulse` | integer ms | Fire big jet for N ms |

Pulses are safe by design: the deadline is always set on the board and the pin turns
off even if the network drops. Sending a longer pulse extends the deadline; a shorter
pulse never shortens an active one.

## Required CircuitPython Libraries

Install from the [CircuitPython Library Bundle](https://circuitpython.org/libraries)
matching CircuitPython 10.x:

- `adafruit_minimqtt` (folder)
- `adafruit_requests.mpy`
- `adafruit_connection_manager.mpy`
- `adafruit_ticks.mpy`

Copy the above into `CIRCUITPY/lib/`.

## Firmware Files (CIRCUITPY root)

| File | Purpose |
|---|---|
| `code.py` | **Active firmware** — non-blocking MQTT GPIO pulse controller |
| `secrets.py` | WiFi + MQTT credentials (copy from `secrets.example.py`, do not commit) |

## Fire Control TUI

See `INSTALL.md` — run as `bush-firecontrol`.

## Rebuild Steps

1. Flash CircuitPython 10.x onto the Pico 2 W.
2. Copy all files from `CIRCUITPY/` in this package to the `CIRCUITPY` drive.
3. Copy `secrets.example.py` to `secrets.py` and fill in credentials.
4. Install libraries listed above into `CIRCUITPY/lib/`.
5. The board auto-starts `code.py` on power-up.

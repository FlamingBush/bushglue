# Valve Control Board

Standalone needle-valve node. Runs the MKS SERVO42C driver (`valve.py`, identical
to the relay-control build except the UART pins) on its own board, off the Pico.
The nRF52840 has BLE but **no Wi-Fi**, so commands arrive over BLE (Nordic UART)
and a host bridges them to MQTT — see `services/core/src/bush_valve_ble/`.

## Hardware

| Field | Value |
|---|---|
| Board | Seeed XIAO nRF52840 Sense |
| Board ID | `Seeed XIAO nRF52840 Sense` |
| CircuitPython | 10.x |
| Radio | BLE only (no Wi-Fi) |

## Pin Assignments

| Pin | Function |
|---|---|
| D6 (`board.TX`) | UART TX → MKS SERVO42C RX (needle valve) |
| D7 (`board.RX`) | UART RX ← MKS SERVO42C TX (needle valve) |
| GND | Common ground with the MKS controller |

3.3 V UART, same as the Pico build. The XIAO does **not** power the motor — the
MKS runs off its own supply with current headroom (~2 A; under-current browns out
the controller and false-homes). Share grounds between the XIAO and the MKS.

## BLE transport + line protocol

Advertises a Nordic UART Service under the name **`bushvalve`**. The host bridge
(`bush-valve-ble`) connects, then tunnels newline-framed `"<topic> <payload>"`
lines — `<topic>` is the literal MQTT topic, split on the first space:

| Direction | Lines |
|---|---|
| host → valve | the `bush/fire/valve/*` command topics: `target`, `home`, `stop`, `calibrate`, `breath`, `maxtorque`, `nudge` |
| valve → host | `bush/fire/valve/actual <frac>` (250 ms), `bush/fire/valve/status <json>` (1 s idle / 200 ms moving) |

`bush/fire/valve/online` is **not** emitted by the firmware — the bridge owns it,
publishing retained `online`/`offline` from the BLE link state. Payload semantics
are unchanged from the Pico build; see `../relay-control/BOARD.md` and `PROTOCOL.md`.

## Required CircuitPython Libraries

Install from the [CircuitPython Library Bundle](https://circuitpython.org/libraries)
matching CircuitPython 10.x:

- `adafruit_ble` (folder)

Easiest with [`circup`](https://github.com/adafruit/circup): `circup install adafruit_ble`.
`_bleio` underneath it is native to the nRF52840 build (nothing to install).

## Firmware Files (CIRCUITPY root)

| File | Purpose |
|---|---|
| `code.py` | **Active firmware** — BLE Nordic-UART ↔ valve glue + main loop |
| `valve.py` | MKS SERVO42C needle-valve driver (UART on `board.TX`/`board.RX`) |

## Rebuild Steps

1. Put the XIAO in its UF2 bootloader (double-tap reset → `XIAO-SENSE` drive) and
   drop the CircuitPython 10.x UF2; it reboots and mounts as `CIRCUITPY`.
2. `circup install adafruit_ble` (or copy `adafruit_ble/` into `CIRCUITPY/lib/`).
3. Copy `code.py` and `valve.py` from this `CIRCUITPY/` to the board.
4. The board auto-starts `code.py` and advertises as `bushvalve`. Power the MKS
   before boot — `valve.init()` blocks on its UART handshake.
5. On the host, run the bridge: `uv run bush-valve-ble` (needs a reachable MQTT
   broker; defaults to `localhost:1883`, override with `BUSH_MQTT_BROKER`).

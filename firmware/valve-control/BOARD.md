# Valve Control Board

Standalone needle-valve node. Runs the **MKS SERVO42D** driver (`valve.py`) on its own
board, off the Pico, talking to the 42D **over CAN** (an MCP2515 on the XIAO's SPI). The
nRF52840 has BLE but **no Wi-Fi**, so commands arrive over BLE (Nordic UART) and a host
bridges them to MQTT — see `services/core/src/bush_valve_ble/`.

> The original 42C was destroyed by reverse power (2026-06-04) and replaced with a 42D.
> The motor link is now **CAN** (MCP2515, 500 kbps): the 42D function codes carry over as
> the CAN data payload (SR_vFOC work mode, current in raw mA, 12-bit-RPM + accel-byte
> motion, 0x31 int48 encoder); homing uses the 42D's native locked-rotor (stallguard)
> sensing but is **disabled** until bench-proven. See `PROTOCOL.md`. The CAN path is **not
> yet fully bench-proven** — values tagged `VERIFY` in `valve.py` (direction sense, current,
> RPM, accel, stall behaviour) must be confirmed on the bench.

## Hardware

| Field | Value |
|---|---|
| Board | Seeed XIAO nRF52840 Sense |
| Board ID | `Seeed XIAO nRF52840 Sense` |
| CircuitPython | 10.x |
| Radio | BLE only (no Wi-Fi) |
| Servo | MKS SERVO42D (CAN via MCP2515 on the XIAO SPI, 500 kbps) |

## Pin Assignments

An MCP2515 SPI-CAN module sits between the XIAO and the 42D. Wire it to the XIAO's hardware
SPI; the transceiver's `CAN_H`/`CAN_L` go to the 42D's CAN port (120 Ω at both bus ends).

| Pin | Function |
|---|---|
| `board.SCK` | SPI clock → MCP2515 SCK |
| `board.MOSI` | SPI MOSI → MCP2515 SI |
| `board.MISO` | SPI MISO ← MCP2515 SO |
| `D3` | MCP2515 CS — any free GPIO; must match `code_xiao_ble.py` |
| GND | Common ground: XIAO ↔ MCP2515 ↔ 42D |

The XIAO does **not** power the motor — the 42D runs off its own 12–24 V supply (to `V+`/`GND`,
**not** `IN_*`). The MCP2515 module is 3.3 V-logic; confirm its crystal (`crystal_freq` in
`code_xiao_ble.py`, default 16 MHz) or the bus silently never ACKs. 42D menu: CAN rate 500 kbps,
CAN ID 1, MStep 16 — the firmware sets SR_vFOC + run current over the bus. **Watch supply
polarity** — reverse power killed the 42C.

## BLE transport + line protocol

Advertises a Nordic UART Service under the name **`bushvalve`**. The host bridge
(`bush-valve-ble`) connects, then tunnels newline-framed `"<topic> <payload>"`
lines — `<topic>` is the literal MQTT topic, split on the first space:

| Direction | Lines |
|---|---|
| host → valve | the `bush/fire/valve/*` command topics: `target`, `home`, `stop`, `calibrate`, `breath`, `maxtorque`, `nudge` |
| valve → host | `bush/fire/valve/actual <frac>` (250 ms), `bush/fire/valve/status <json>` (1 s idle / 200 ms moving) |

`bush/fire/valve/online` is **not** emitted by the firmware — the bridge owns it,
publishing retained `online`/`offline` from the BLE link state. The line-protocol
topics/payloads are unchanged, with one 42D semantic shift: **`maxtorque` now sets
the run current in mA** (0–3000), since the 42D has no 42C-style torque cap and
bounds force via current. See `PROTOCOL.md`.

## Required CircuitPython Libraries

Install from the [CircuitPython Library Bundle](https://circuitpython.org/libraries)
matching CircuitPython 10.x:

- `adafruit_ble` (folder) — BLE host link
- `adafruit_mcp2515` (folder) — the CAN motor link
- `adafruit_bus_device` (folder) — dependency of `adafruit_mcp2515`

Easiest with [`circup`](https://github.com/adafruit/circup):
`circup install adafruit_ble adafruit_mcp2515`. `_bleio` underneath the BLE lib is native to
the nRF52840 build (nothing to install).

## Firmware Files (CIRCUITPY root)

| File | Purpose |
|---|---|
| `code_xiao_ble.py` | **Active firmware** (deploy as `code.py`) — BLE Nordic-UART ↔ valve glue + main loop; builds the MCP2515 |
| `valve.py` | MKS SERVO42D needle-valve driver (CAN; shared with the Pico 2 W / Wi-Fi node) |

## Rebuild Steps

1. Put the XIAO in its UF2 bootloader (double-tap reset → `XIAO-SENSE` drive) and
   drop the CircuitPython 10.x UF2; it reboots and mounts as `CIRCUITPY`.
2. `circup install adafruit_ble adafruit_mcp2515` (or copy the folders into `CIRCUITPY/lib/`).
3. Copy `valve.py` and `code_xiao_ble.py` **as `code.py`** to the board.
4. The board auto-starts `code.py` and advertises as `bushvalve`. Power the 42D + MCP2515
   before boot — `valve.init()` blocks on its CAN handshake.
5. On the host, run the bridge: `uv run bush-valve-ble` (needs a reachable MQTT
   broker; defaults to `localhost:1883`, override with `BUSH_MQTT_BROKER`).

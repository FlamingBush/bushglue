# PIO-CAN demonstrator (Pico 2 W → MKS SERVO42D, no MCP2515)

> **SUPERSEDED (2026-06-06):** CAN was abandoned — the MCP2515 controller died, and the valve node
> now drives the 42D as a plain stepper over **STEP/DIR** (no CAN, no transceiver, no encoder). This
> demonstrator is kept for history only. See `firmware/valve-control/PROTOCOL.md` (step/dir section)
> and `plans/servo42d-bringup.md`.

Goal: prove the RP2350 can talk CAN to the 42D using **PIO + a bare transceiver** (no MCP2515
controller) by sending one MKS command and seeing the reply (and optionally a slow motor turn).
This de-risks a controller-less path before committing to rewriting the valve node.

## Reality / why this is a separate demonstrator
- Pure-Python bit-bang can't meet CAN timing. The working PIO-CAN is **can2040** (Kevin O'Connor),
  which is **C** — CircuitPython has no PIO-CAN binding, so this demonstrator is a small **C-SDK app**,
  separate from the CircuitPython bush-cue firmware. (MicroPython + a can2040 wrapper also works but
  is less turnkey.)
- You still need a **CAN transceiver** (the bus is differential). can2040 replaces the MCP2515
  *controller*, not the transceiver. Use a 3.3 V transceiver: SN65HVD230 / TJA1051T/3 / MCP2562FD.

## Hardware
- Pico 2 W + transceiver: Pico **TX GPIO → transceiver TXD**, **RX GPIO ← transceiver RXD**,
  transceiver **VCC 3.3 V**, **GND common with Pico AND the 42D**. Transceiver **CANH/CANL → 42D
  CANH/CANL**, **120 Ω** across CANH/CANL at the ends. 42D power: **12–24 V → V+/GND** (not IN_*).
- Pins: can2040 takes any two GPIOs; reuse the wired ones — e.g. `gpio_tx = 6`, `gpio_rx = 4`.

## Software (C SDK + can2040) — minimal main
1. Project: pico-sdk + can2040 as a submodule (`git submodule add https://github.com/KevinOConnor/can2040`).
   CMake: add `can2040/src/can2040.c`, include its header; `pico_enable_stdio_usb(... 1)` for serial.
2. Init (RP2350): 
   - `can2040_setup(&cbus, 0)` (PIO 0); `can2040_callback_config(&cbus, can_cb)`.
   - Wire the PIO IRQ: in `PIO0_IRQ0` handler call `can2040_pio_irq_handler(&cbus)`; enable it.
   - `can2040_start(&cbus, clock_get_hz(clk_sys), 500000, gpio_rx=4, gpio_tx=6)`.
3. MKS frame helper (see PROTOCOL.md CAN section): `id = 1`; data = `[func, params…, crc]`,
   `crc = (id + func + params) & 0xFF`; standard 11-bit id (no ext/RTR). 24-bit pulses for 0xFD.
4. **Test A — read encoder (safe, no motion):** send `0x31`: `data=[0x31, 0x32]` (crc=(1+0x31)&0xFF),
   `dlc=2`. In `can_cb` on `CAN2040_NOTIFY_RX`, print `msg.id` + `msg.data` over USB. Expect the 42D
   reply `[0x31, <int48 6 B>, crc]` (dlc 8). A reply = CAN round-trip proven.
5. **Test B — slow turn (optional, low current, motor free/uncoupled):** SR_vFOC + low current first
   (`0x82 05`, `0x83 <mA>`), `0xF3 01` (enable), then `0xF6` at a low RPM toward open for ~1 s, then
   `0xF6` speed 0 + `0xF3 00`. Confirms TX path + the motor moves.

## Build / flash (sidesteps the FSKit drive mess)
- `cmake -B build && make` → `build/<app>.uf2`.
- Hold **BOOTSEL**, plug in → mounts as **RPI-RP2**; copy the `.uf2` (Finder drag, or it's a one-file
  copy). Board reboots into the C app. Watch USB serial (`/dev/cu.usbmodem*`, e.g. via `screen` or a
  read loop) for the encoder reply.

## Verify / decision
- Pass = the `0x31` reply prints (and/or the motor turns in Test B). That proves PIO-CAN + transceiver
  talks to this 42D — the controller-less approach is viable.
- Then choose the production path: (a) **keep CircuitPython + MCP2515** (the existing firmware already
  runs to the CAN init; it's one SPI wire from working), or (b) **port the valve node to C (or
  MicroPython) + can2040** — bigger, but no MCP2515. The demonstrator tells you (b) is worth it before
  you pay for it.

Source: can2040 — github.com/KevinOConnor/can2040 (RP2040/RP2350, ≤1 Mbit, needs a transceiver).

# demo_breath.py -- BENCH DEMO, no BLE. Copy onto the board AS code.py to auto-run.
#
# There's no valve attached, so homing (which drives into the closed seat) is
# DISABLED. Instead we read the encoder once, declare the current shaft position
# to be mid-travel, and run valve.py's real breathing routine -- the bare MKS
# motor gently oscillates. Everything below valve.py is unchanged; this only
# stands in for the normal home->idle->breathe entry that BLE/MQTT would drive.

import struct
import supervisor
import time

import valve

# No physical endstops without a valve, so pick a travel range that breathes
# visibly (the firmware default 2000 rounds the breath velocity to gear 0) and
# sit at its middle so the oscillation never clamps at an edge.
DEMO_OPEN_STEPS = 16000
MID = DEMO_OPEN_STEPS // 2


def _read_encoder_raw(timeout_ms=600):
    """Blocking 0x30 read (motor idle/stopped -> reliable). Raw multi-turn counts, or None."""
    valve._blocking_drain()
    valve._send(bytes([valve.CMD_READ_ENCODER]))
    deadline = (supervisor.ticks_ms() + timeout_ms) & 0x3FFFFFFF
    buf = bytearray()
    while valve._ticks_diff(supervisor.ticks_ms(), deadline) < 0x1FFFFFFF:
        n = valve.uart.in_waiting
        if n:
            buf.extend(valve.uart.read(n))
        while len(buf) >= 8:
            if buf[0] != valve.MKS_ADDR:
                del buf[0]
                continue
            if valve._checksum(buf[0:7]) != buf[7]:
                del buf[0]
                continue
            carry = struct.unpack(">i", bytes(buf[1:5]))[0]
            value = struct.unpack(">H", bytes(buf[5:7]))[0]
            return (carry << 16) | value
        time.sleep(0.005)
    return None


def _fake_home(raw):
    """Seed the absolute-position reference WITHOUT homing: map the current shaft
    angle to mid-travel, mark homed, sit idle ready to breathe."""
    valve.open_steps = DEMO_OPEN_STEPS
    valve._enc_sign = 1
    valve._enc_zero_raw = raw - int(round(MID * valve.ENC_PER_STEP))
    valve.motor_pos_steps = MID
    valve.target_pos_steps = MID
    valve.homed = True
    valve.state = "idle"


def main():
    print("DEMO: breathing, no BLE, homing DISABLED (no valve attached)")
    valve.init()
    if valve.state == "error":
        print("DEMO: valve.init failed -- check MKS power + UART (D6/D7). Halting.")
        return
    raw = _read_encoder_raw()
    if raw is None:
        print("DEMO: no encoder response -- check MKS power + UART wiring. Halting.")
        return
    _fake_home(raw)
    print(f"DEMO: faux-home seeded raw={raw} pos={MID}/{DEMO_OPEN_STEPS} -- breathing now")
    valve._breath_enabled = True
    valve._enter_breathing(supervisor.ticks_ms())
    try:
        while True:
            valve.service()
    finally:
        valve._send(bytes([valve.CMD_CONSTANT_SPEED, 0x00]))
        valve._send(bytes([valve.CMD_STOP]))
        valve._send(bytes([valve.CMD_ENABLE, 0x00]))
        print("DEMO: stopped, motor de-energized")


main()

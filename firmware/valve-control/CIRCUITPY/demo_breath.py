# demo_breath.py -- BENCH DEMO (no MQTT). Copy onto the board AS code.py to auto-run.
#
# There's no valve attached, so homing (which drives into the closed seat) is
# DISABLED. Instead we read the encoder once, declare the current shaft position
# to be mid-travel, and run valve.py's real breathing routine -- the bare MKS
# motor gently oscillates. Everything below valve.py is unchanged; this only
# stands in for the normal home->idle->breathe entry that MQTT would drive.
#
# Targets the MKS SERVO42D over CAN (see valve.py / PROTOCOL.md). Needs an MCP2515
# (circup install adafruit_mcp2515). Edit the CAN config block for your board.

import board
import busio
import digitalio
import supervisor

from adafruit_mcp2515 import MCP2515 as CAN
from adafruit_mcp2515.canio import Message

import valve

# CAN config -- match your MCP2515 wiring (mirror code.py).
CAN_SCK, CAN_MOSI, CAN_MISO, CAN_CS = board.GP18, board.GP19, board.GP16, board.GP17
CAN_BITRATE, CAN_CRYSTAL = 500000, 16_000_000   # 8_000_000 for a generic 8 MHz module

_spi = busio.SPI(CAN_SCK, CAN_MOSI, CAN_MISO)
_cs = digitalio.DigitalInOut(CAN_CS)
_cs.switch_to_output(True)
valve.Message = Message
valve.can = CAN(_spi, _cs, baudrate=CAN_BITRATE, crystal_freq=CAN_CRYSTAL)

# No physical endstops without a valve, so pick a travel range that breathes
# visibly (the firmware default 2000 rounds the breath velocity to ~0 RPM) and
# sit at its middle so the oscillation never clamps at an edge.
DEMO_OPEN_STEPS = 16000
MID = DEMO_OPEN_STEPS // 2


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
    print("DEMO: breathing over CAN, no MQTT, homing DISABLED (no valve attached)")
    valve.init()
    if valve.state == "error":
        print("DEMO: valve.init failed -- check 42D power, CAN wiring/termination, "
              "motor CAN ID, bitrate 500k, crystal_freq. Halting.")
        return
    raw = valve._blocking_read_encoder()
    if raw is None:
        print("DEMO: no encoder (0x31) reply -- check CAN wiring + MCP2515 config. Halting.")
        return
    _fake_home(raw)
    print(f"DEMO: faux-home seeded raw={raw} pos={MID}/{DEMO_OPEN_STEPS} -- breathing now")
    valve._breath_enabled = True
    valve._enter_breathing(supervisor.ticks_ms())
    try:
        while True:
            valve.service()
    finally:
        valve._send(bytes([valve.CMD_CONSTANT_SPEED, 0x00, 0x00, 0x00]))
        valve._send(bytes([valve.CMD_STOP]))
        valve._send(bytes([valve.CMD_ENABLE, 0x00]))
        print("DEMO: stopped, motor de-energized")


main()

# demo_breath.py -- BENCH DEMO, no BLE. Copy onto the board AS code.py to auto-run.
#
# There's no valve attached, so homing (which drives into the closed seat) is
# DISABLED. Instead we read the encoder once, declare the current shaft position
# to be mid-travel, and run valve.py's real breathing routine -- the bare MKS
# motor gently oscillates. Everything below valve.py is unchanged; this only
# stands in for the normal home->idle->breathe entry that BLE/MQTT would drive.
#
# Targets the MKS SERVO42D (see valve.py).

import supervisor

import valve

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
    print("DEMO: breathing, no BLE, homing DISABLED (no valve attached)")
    valve.init()
    if valve.state == "error":
        print("DEMO: valve.init failed -- check 42D power, SR_vFOC, baud 38400, UART (D6/D7). Halting.")
        return
    raw = valve._blocking_read_encoder()
    if raw is None:
        print("DEMO: no encoder (0x31) response -- check 42D power + UART wiring. Halting.")
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

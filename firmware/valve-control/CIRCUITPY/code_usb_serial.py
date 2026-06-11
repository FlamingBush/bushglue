# code.py -- USB-serial valve node (radio-less RP2350 carriers: Waveshare RP2350-CAN,
# or a reflashed BridgePlate). Runs the MKS needle-valve driver (valve.py) and exposes
# it over a USB CDC "data" channel as newline-framed "<topic> <payload>" lines:
#   in : the bush/fire/valve/* command topics (target/home/stop/calibrate/breath/maxtorque/nudge)
#        + binary stream frames (0xF5 sentinel) for bush-cue waveform playback
#   out: bush/fire/valve/actual, bush/fire/valve/status
# A host bridges these lines to/from MQTT (services/core/src/bush_valve_serial); valve.py's
# (topic, payload) interface IS the line protocol, so there's no translation here.
#
# Requires boot.py to have enabled the second CDC: usb_cdc.enable(console=True, data=True).
# The REPL stays on `console` for debugging; this protocol runs on the clean `data` channel.

import os
import board
import busio
import digitalio
import usb_cdc

from adafruit_mcp2515 import MCP2515 as CAN
from adafruit_mcp2515.canio import Message

import valve

# CAN carrier SPI pins + XL2515/MCP2515 crystal, per board. Crystal MUST match the module
# or the bus silently never ACKs. Pick via settings.toml `BOARD=`; default to the Waveshare.
BOARD_PROFILES = {
    # Waveshare RP2350-CAN (onboard XL2515 @ 16 MHz, SIT65HVD230 3.3 V transceiver) -- schematic-verified.
    "waveshare": {"clk": board.GP10, "mosi": board.GP11, "miso": board.GP12, "cs": board.GP9, "crystal": 16_000_000},
    # Phase 2: CanBerry MCP2515 on a reflashed BridgePlate's SPI0 header. Fill the GP map
    # from the BridgePlate schematic / dir(board) once that hardware is on the bench.
    # "canberry": {"clk": board.GPxx, "mosi": board.GPxx, "miso": board.GPxx, "cs": board.GPxx, "crystal": 16_000_000},
}
CAN_BITRATE = 500_000  # 42D menu: CAN 500 kbps, CAN ID 1, MStep 16 (init() sets SR_vFOC + current).

_board = os.getenv("BOARD") or "waveshare"
_prof = BOARD_PROFILES[_board]
_can_spi = busio.SPI(_prof["clk"], _prof["mosi"], _prof["miso"])
_can_cs = digitalio.DigitalInOut(_prof["cs"])
_can_cs.switch_to_output(True)
valve.Message = Message
valve.can = CAN(_can_spi, _can_cs, baudrate=CAN_BITRATE, crystal_freq=_prof["crystal"])

# The data CDC. Non-blocking both ways so a slow/absent host never stalls valve.service()
# (the solenoid-OFF and breath deadlines must always hold).
_serial = usb_cdc.data
if _serial is not None:
    _serial.timeout = 0
    _serial.write_timeout = 0

_rx = bytearray()


def _dispatch(line):
    line = bytes(line).strip()
    if not line:
        return
    sp = line.find(b" ")
    if sp < 0:
        topic, payload = line, b""
    else:
        topic, payload = line[:sp], line[sp + 1:]
    if topic in valve.ALL_VALVE_TOPICS:
        valve.handle_mqtt(topic, payload)


def _read_commands():
    n = _serial.in_waiting
    if n:
        data = _serial.read(n)
        if data:
            _rx.extend(data)
    # Two interleaved framings on one byte stream: binary stream frames start with
    # valve.STREAM_SENTINEL (0xF5, never the start of a text topic line); everything
    # else is a newline-framed "<topic> <payload>" line.
    while _rx:
        if _rx[0] == valve.STREAM_SENTINEL:
            if len(_rx) < 4:
                break
            ln = (_rx[2] << 8) | _rx[3]
            total = 4 + ln + 1
            if len(_rx) < total:
                break
            frame = _rx[:total]
            if (sum(frame[:-1]) & 0xFF) == frame[-1]:
                valve.handle_stream(frame[1], bytes(frame[4:4 + ln]))
                _rx[:] = _rx[total:]
            else:
                _rx[:] = _rx[1:]   # bad checksum -- drop one byte and resync
        else:
            nl = _rx.find(b"\n")
            if nl < 0:
                break
            line = _rx[:nl]
            _rx[:] = _rx[nl + 1:]
            _dispatch(line)


def _write_telemetry():
    for topic, payload in valve.get_publish_messages():
        if isinstance(payload, str):
            payload = payload.encode("utf-8")
        try:
            _serial.write(topic + b" " + payload + b"\n")
        except OSError:
            return  # host not draining; drop and move on (write_timeout=0)


def main():
    if _serial is None:
        # No data CDC -> boot.py didn't run usb_cdc.enable(data=True). Drive the motor
        # anyway (keep it safe/idle), but there's no host link until boot.py is fixed.
        print("valve usb-serial: usb_cdc.data is None -- add boot.py and hard-reset")
    print("Valve node: USB-serial (BOARD=%s) -- init" % _board)
    valve.init()
    was_connected = False
    while True:
        valve.service()
        if _serial is None:
            continue
        connected = _serial.connected
        if connected:
            if not was_connected:
                _serial.reset_input_buffer()
                _rx[:] = b""
                print("USB-serial: host connected")
            _read_commands()
            _write_telemetry()
        elif was_connected:
            print("USB-serial: host disconnected")
        was_connected = connected


main()

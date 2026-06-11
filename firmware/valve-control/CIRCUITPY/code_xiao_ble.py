# code.py -- XIAO nRF52840 valve node. Runs the MKS needle-valve driver (valve.py)
# and exposes it over BLE Nordic UART. Newline-framed "<topic> <payload>" lines:
#   in : the bush/fire/valve/* command topics (target/home/stop/calibrate/breath/maxtorque/nudge)
#   out: bush/fire/valve/actual, bush/fire/valve/status
# A host bridges these lines to/from MQTT (services/core/src/bush_valve_ble); valve.py's
# (topic, payload) interface IS the line protocol, so there's no translation here.

import board
import busio
import digitalio

from adafruit_mcp2515 import MCP2515 as CAN
from adafruit_mcp2515.canio import Message

from adafruit_ble import BLERadio
from adafruit_ble.advertising import Advertisement
from adafruit_ble.advertising.standard import ProvideServicesAdvertisement
from adafruit_ble.services.nordic import UARTService

import valve

# valve.py is the CAN closed-loop driver: wire an MCP2515 to the XIAO's hardware SPI
# (SCK/MOSI/MISO) + a free GPIO for CS, and assign valve.can + valve.Message before
# valve.init(). Crystal MUST match the module (16 vs 8 MHz). 42D menu: CAN rate 500 kbps,
# CAN ID 1, MStep 16 (init() sets SR_vFOC + run current over the bus). (UART/STEP-DIR retired.)
_can_spi = busio.SPI(board.SCK, board.MOSI, board.MISO)
_can_cs = digitalio.DigitalInOut(board.D3)   # CS — any free XIAO GPIO; match your wiring
_can_cs.switch_to_output(True)
valve.Message = Message
valve.can = CAN(_can_spi, _can_cs, baudrate=500_000, crystal_freq=16_000_000)

BLE_NAME = "bushvalve"

ble = BLERadio()
ble.name = BLE_NAME
_uart = UARTService()
# The 128-bit NUS UUID (18 B) + flags (3 B) + the complete name (11 B) = 32 B, one over
# the 31-byte advertisement limit -> start_advertising would raise and never advertise.
# Keep the service UUID in the main packet and put the name in the scan response.
_adv = ProvideServicesAdvertisement(_uart)
_scan_response = Advertisement()
_scan_response.complete_name = BLE_NAME

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
    n = _uart.in_waiting
    if n:
        data = _uart.read(n)
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
            _uart.write(topic + b" " + payload + b"\n")
        except OSError:
            return  # central dropped mid-write; the reconnect path handles it


def main():
    print("Valve node: BLE name", BLE_NAME, "-- init")
    valve.init()
    advertising = False
    was_connected = False
    while True:
        valve.service()
        connected = ble.connected
        if connected:
            if advertising:
                ble.stop_advertising()
                advertising = False
            if not was_connected:
                _uart.reset_input_buffer()
                _rx[:] = b""
                print("BLE: central connected")
            _read_commands()
            _write_telemetry()
        else:
            if was_connected:
                print("BLE: central disconnected")
            if not advertising:
                ble.start_advertising(_adv, scan_response=_scan_response)
                advertising = True
        was_connected = connected


main()

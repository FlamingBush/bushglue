# code.py -- XIAO nRF52840 valve node. Runs the MKS needle-valve driver (valve.py)
# and exposes it over BLE Nordic UART. Newline-framed "<topic> <payload>" lines:
#   in : the bush/fire/valve/* command topics (target/home/stop/calibrate/breath/maxtorque/nudge)
#   out: bush/fire/valve/actual, bush/fire/valve/status
# A host bridges these lines to/from MQTT (services/core/src/bush_valve_ble); valve.py's
# (topic, payload) interface IS the line protocol, so there's no translation here.

from adafruit_ble import BLERadio
from adafruit_ble.advertising.standard import ProvideServicesAdvertisement
from adafruit_ble.services.nordic import UARTService

import valve

BLE_NAME = "bushvalve"

ble = BLERadio()
ble.name = BLE_NAME
_uart = UARTService()
_adv = ProvideServicesAdvertisement(_uart)
_adv.complete_name = BLE_NAME

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
    while True:
        nl = _rx.find(b"\n")
        if nl < 0:
            break
        line = _rx[:nl]
        del _rx[:nl + 1]
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
                del _rx[:]
                print("BLE: central connected")
            _read_commands()
            _write_telemetry()
        else:
            if was_connected:
                print("BLE: central disconnected")
            if not advertising:
                ble.start_advertising(_adv)
                advertising = True
        was_connected = connected


main()

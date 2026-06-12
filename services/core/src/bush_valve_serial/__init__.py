#!/usr/bin/env python3
"""USB-serial <-> MQTT bridge for a radio-less RP2350 valve node.

The needle-valve driver runs on an RP2350 carrier with no radio (Waveshare
RP2350-CAN, or a reflashed BridgePlate) and speaks a newline-framed
"<topic> <payload>" line protocol over a USB CDC "data" channel. This bridges
those lines to/from the existing bush/fire/valve/* MQTT topics, so the rest of
the pipeline (bush_variable_valves, bush-monitor) is unchanged:

  MQTT command topics       --> CDC-data write --> firmware valve.handle_mqtt
  firmware get_publish_messages --> CDC-data read  --> MQTT bush/fire/valve/{actual,status}

This is the serial twin of bush_valve_ble: same line protocol, same online
ownership, transport swapped from BLE (bleak) to pyserial.

`online` is owned here, derived from the serial link: retained "online" when the
port opens, "offline" when it drops (and via the MQTT LWT if this bridge dies).

Port selection:
  - BUSH_VALVE_SERIAL_PORT, if set, is used verbatim (recommended for deploy --
    point it at a stable /dev/serial/by-id/...-if02 path on Linux).
  - otherwise the data CDC is auto-detected: a CircuitPython board exposes two
    CDC ports sharing one USB serial number; console is the lower interface,
    data is the higher. BUSH_VALVE_SERIAL_SERIAL disambiguates multiple boards.

Broker defaults to bushutil.get_mqtt_broker(); set BUSH_MQTT_BROKER to override.
"""

import os
import queue
import re
import signal
import sys
import threading
import time

import paho.mqtt.client as mqtt
import serial
from serial.tools import list_ports

from bushutil import get_mqtt_broker

MQTT_PORT = 1883
SERIAL_BAUD = 115200  # nominal; USB CDC ignores the rate, pyserial just needs a value.

# CircuitPython USB vendor IDs (generic raspberry_pi_pico2 build = Raspberry Pi; Adafruit boards).
CP_VIDS = {0x2E8A, 0x239A}

TOPIC_ONLINE = "bush/fire/valve/online"

# Raw binary stream frames (bush-cue waveform playback). The payload IS the frame
# bytes; we pass them straight through (the firmware length-frames them).
TOPIC_STREAM = "bush/fire/valve/stream"

# Command topics forwarded host -> valve (mirror valve.ALL_VALVE_TOPICS).
COMMAND_TOPICS = [
    "bush/fire/valve/target",
    "bush/fire/valve/home",
    "bush/fire/valve/stop",
    "bush/fire/valve/calibrate",
    "bush/fire/valve/breath",
    "bush/fire/valve/maxtorque",
    "bush/fire/valve/nudge",
    "bush/fire/valve/limits",
    "bush/fire/valve/trace",
    TOPIC_STREAM,
]

OPEN_RETRY_BACKOFF_S = 3.0


def log(msg: str):
    print(f"[valve-serial] {msg}", flush=True)


def _natural_tail(dev: str) -> int:
    m = re.search(r"(\d+)$", dev)
    return int(m.group(1)) if m else -1


def find_serial_port():
    """Return the data-CDC device path, or None if no candidate board is present."""
    override = os.environ.get("BUSH_VALVE_SERIAL_PORT")
    if override:
        return override

    want_serial = os.environ.get("BUSH_VALVE_SERIAL_SERIAL")
    ports = list(list_ports.comports())
    cands = [p for p in ports if p.vid in CP_VIDS or (want_serial and p.serial_number == want_serial)]
    if want_serial:
        cands = [p for p in cands if p.serial_number == want_serial]
    if not cands:
        return None

    by_serial: dict = {}
    for p in cands:
        by_serial.setdefault(p.serial_number, []).append(p)

    # Prefer a board exposing >=2 CDC ports (console + data); the data CDC is the
    # higher interface, which sorts last by the trailing device number.
    group = None
    if want_serial and want_serial in by_serial:
        group = by_serial[want_serial]
    else:
        for ps in by_serial.values():
            if len(ps) >= 2:
                group = ps
                break
        if group is None:
            group = next(iter(by_serial.values()))

    group = sorted(group, key=lambda p: _natural_tail(p.device))
    if len(group) < 2:
        log(f"warning: only one CDC port for board {group[0].serial_number} -- "
            f"is boot.py enabling usb_cdc data? using {group[-1].device}")
    return group[-1].device


def _wait_for_port(stop: "threading.Event"):
    announced = False
    while not stop.is_set():
        port = find_serial_port()
        if port is not None:
            log(f"using data port {port}")
            return port
        if not announced:
            log("waiting for valve node (no CircuitPython CDC found)...")
            announced = True
        stop.wait(OPEN_RETRY_BACKOFF_S)
    return None


def _drain(q: "queue.Queue"):
    try:
        while True:
            q.get_nowait()
    except queue.Empty:
        pass


def _serve(mqttc, port, cmd_queue, stop):
    """One serial session: forward MQTT commands to the valve and republish its
    telemetry. Returns when the link drops or stop is set."""
    try:
        ser = serial.Serial(port, SERIAL_BAUD, timeout=0, write_timeout=1.0)
    except (OSError, serial.SerialException) as e:
        log(f"open {port} failed: {e}")
        return
    log(f"connected to {port}")
    mqttc.publish(TOPIC_ONLINE, "online", qos=1, retain=True)
    rx = bytearray()
    try:
        while not stop.is_set():
            progressed = False
            # valve -> host: read available bytes, publish complete lines
            try:
                n = ser.in_waiting
                data = ser.read(n) if n else b""
            except (OSError, serial.SerialException) as e:
                log(f"serial read error: {e}")
                break
            if data:
                rx.extend(data)
                progressed = True
                while True:
                    nl = rx.find(b"\n")
                    if nl < 0:
                        break
                    line = bytes(rx[:nl]).strip()
                    del rx[: nl + 1]
                    if not line:
                        continue
                    sp = line.find(b" ")
                    topic = line if sp < 0 else line[:sp]
                    payload = b"" if sp < 0 else line[sp + 1:]
                    try:
                        mqttc.publish(topic.decode("ascii"), payload, qos=0)
                    except Exception as e:
                        log(f"mqtt publish error: {e}")
            # host -> valve: drain queued commands / stream frames
            try:
                while True:
                    item = cmd_queue.get_nowait()
                    progressed = True
                    if isinstance(item, (bytes, bytearray)):
                        ser.write(item)                       # raw stream frame
                    else:
                        ser.write((item + "\n").encode("utf-8"))
            except queue.Empty:
                pass
            except (OSError, serial.SerialException) as e:
                log(f"serial write error: {e}")
                break
            if not progressed:
                time.sleep(0.005)
    finally:
        try:
            ser.close()
        except Exception:
            pass
    log("serial session closed")


def _run(mqttc, broker):
    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, lambda *_: stop.set())
        except ValueError:
            pass  # not the main thread

    cmd_queue: "queue.Queue" = queue.Queue(maxsize=256)

    def on_message(client, userdata, msg):
        if msg.topic == TOPIC_STREAM:
            item = bytes(msg.payload)  # raw frame
        else:
            payload = msg.payload.decode("utf-8", "ignore").strip()
            item = msg.topic if not payload else f"{msg.topic} {payload}"
        try:
            cmd_queue.put_nowait(item)
        except queue.Full:
            pass  # drop stale commands rather than back up

    mqttc.on_message = on_message

    try:
        mqttc.connect(broker, MQTT_PORT, 60)
    except Exception as e:
        log(f"cannot connect to broker: {e}")
        return
    mqttc.loop_start()
    try:
        while not stop.is_set():
            port = _wait_for_port(stop)
            if port is None:
                break
            try:
                _serve(mqttc, port, cmd_queue, stop)
            except Exception as e:
                log(f"serial session error: {e}")
            mqttc.publish(TOPIC_ONLINE, "offline", qos=1, retain=True)
            _drain(cmd_queue)  # don't replay stale commands on reconnect
            if not stop.is_set():
                stop.wait(OPEN_RETRY_BACKOFF_S)
    finally:
        mqttc.publish(TOPIC_ONLINE, "offline", qos=1, retain=True)
        mqttc.loop_stop()
        mqttc.disconnect()


def main():
    broker = os.environ.get("BUSH_MQTT_BROKER") or get_mqtt_broker()
    log(f"MQTT broker: {broker}:{MQTT_PORT}")

    mqttc = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
    mqttc.will_set(TOPIC_ONLINE, "offline", qos=1, retain=True)

    def on_connect(client, userdata, flags, reason_code, properties):
        log(f"MQTT connected (rc={reason_code})")
        for t in COMMAND_TOPICS:
            client.subscribe(t)
        log(f"subscribed to {len(COMMAND_TOPICS)} valve command topics")

    mqttc.on_connect = on_connect

    try:
        _run(mqttc, broker)
    except KeyboardInterrupt:
        pass
    log("stopped")


if __name__ == "__main__":
    main()

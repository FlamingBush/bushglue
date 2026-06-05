#!/usr/bin/env python3
"""BLE <-> MQTT bridge for the XIAO nRF52840 valve node.

The needle-valve driver runs on a XIAO nRF52840 (BLE, no Wi-Fi) and speaks a
newline-framed "<topic> <payload>" line protocol over a Nordic UART Service.
This bridges those lines to/from the existing bush/fire/valve/* MQTT topics, so
the rest of the pipeline (bush_variable_valves, bush-monitor) is unchanged:

  MQTT command topics  --> BLE NUS RX --> XIAO valve.handle_mqtt
  XIAO get_publish_messages --> BLE NUS TX --> MQTT bush/fire/valve/{actual,status}

`online` is owned here, derived from the BLE link: retained "online" on connect,
"offline" on disconnect (and via the MQTT LWT if this bridge itself dies).

Broker defaults to bushutil.get_mqtt_broker() (localhost on a dev box); set
BUSH_MQTT_BROKER to point elsewhere (e.g. the odroid's mosquitto). Runs locally
for now; the same code later moves onto the odroid unchanged.
"""

import asyncio
import os
import signal
import sys

import paho.mqtt.client as mqtt
from bleak import BleakClient, BleakScanner

from bushutil import get_mqtt_broker

MQTT_PORT = 1883
BLE_NAME = "bushvalve"

# Nordic UART Service (as advertised by adafruit_ble's UARTService on the XIAO).
NUS_SERVICE = "6e400001-b5a3-f393-e0a9-e50e24dcca9e"
NUS_RX_CHAR = "6e400002-b5a3-f393-e0a9-e50e24dcca9e"  # write : host -> valve
NUS_TX_CHAR = "6e400003-b5a3-f393-e0a9-e50e24dcca9e"  # notify: valve -> host

TOPIC_ONLINE = "bush/fire/valve/online"

# Raw binary stream frames (bush-cue waveform playback). The payload IS the frame
# bytes; we pass them through to NUS RX unchanged (the firmware length-frames them).
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
    TOPIC_STREAM,
]

SCAN_TIMEOUT_S = 5.0
RECONNECT_BACKOFF_S = 3.0


def log(msg: str):
    print(f"[valve-ble] {msg}", flush=True)


def _enqueue(q: "asyncio.Queue", item):
    try:
        q.put_nowait(item)
    except asyncio.QueueFull:
        pass  # drop stale commands rather than back up the queue


def _drain(q: "asyncio.Queue"):
    try:
        while True:
            q.get_nowait()
    except asyncio.QueueEmpty:
        pass


async def _find_device(stop: "asyncio.Event"):
    """Scan until a valve node is found (by advertised name or NUS UUID) or stop."""
    while not stop.is_set():
        log("scanning for valve node...")
        try:
            device = await BleakScanner.find_device_by_filter(
                lambda d, ad: ad.local_name == BLE_NAME
                or NUS_SERVICE in (ad.service_uuids or []),
                timeout=SCAN_TIMEOUT_S,
            )
        except Exception as e:  # adapter hiccup, BlueZ error, etc.
            log(f"scan error: {e}")
            device = None
        if device is not None:
            log(f"found {device.address} ({device.name})")
            return device
        if not stop.is_set():
            await asyncio.sleep(RECONNECT_BACKOFF_S)
    return None


async def _serve(mqttc, device, cmd_queue, stop):
    """One BLE session: forward MQTT commands to the valve and republish its
    telemetry. Returns when the link drops or stop is set."""
    loop = asyncio.get_running_loop()
    rx_buf = bytearray()
    disconnected = asyncio.Event()

    def handle_tx(_char, data):
        rx_buf.extend(data)
        while True:
            nl = rx_buf.find(b"\n")
            if nl < 0:
                break
            line = bytes(rx_buf[:nl]).strip()
            del rx_buf[: nl + 1]
            if not line:
                continue
            sp = line.find(b" ")
            topic = line if sp < 0 else line[:sp]
            payload = b"" if sp < 0 else line[sp + 1:]
            try:
                mqttc.publish(topic.decode("ascii"), payload, qos=0)
            except Exception as e:
                log(f"mqtt publish error: {e}")

    def on_disc(_client):
        loop.call_soon_threadsafe(disconnected.set)

    async with BleakClient(device, disconnected_callback=on_disc) as client:
        log(f"connected to {device.address}")
        await client.start_notify(NUS_TX_CHAR, handle_tx)
        mqttc.publish(TOPIC_ONLINE, "online", qos=1, retain=True)
        while not stop.is_set() and not disconnected.is_set():
            try:
                item = await asyncio.wait_for(cmd_queue.get(), timeout=0.5)
            except asyncio.TimeoutError:
                continue
            try:
                if isinstance(item, (bytes, bytearray)):
                    # Binary stream frame: chunk to 20 B (universal -- the firmware
                    # reassembles by length); write-with-response so none are dropped.
                    for i in range(0, len(item), 20):
                        await client.write_gatt_char(NUS_RX_CHAR, item[i:i + 20], response=True)
                else:
                    await client.write_gatt_char(
                        NUS_RX_CHAR, (item + "\n").encode("utf-8"), response=False
                    )
            except Exception as e:
                log(f"ble write error: {e}")
                break
    log("BLE session closed")


async def _run(mqttc, broker):
    loop = asyncio.get_running_loop()
    stop = asyncio.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:
            pass  # e.g. on Windows; falls back to KeyboardInterrupt

    cmd_queue: "asyncio.Queue" = asyncio.Queue(maxsize=256)

    def on_message(client, userdata, msg):
        # paho network thread -> hand the command to the asyncio loop
        if msg.topic == TOPIC_STREAM:
            loop.call_soon_threadsafe(_enqueue, cmd_queue, bytes(msg.payload))  # raw frame
            return
        payload = msg.payload.decode("utf-8", "ignore").strip()
        line = msg.topic if not payload else f"{msg.topic} {payload}"
        loop.call_soon_threadsafe(_enqueue, cmd_queue, line)

    mqttc.on_message = on_message

    try:
        mqttc.connect(broker, MQTT_PORT, 60)
    except Exception as e:
        log(f"cannot connect to broker: {e}")
        return
    mqttc.loop_start()
    try:
        while not stop.is_set():
            device = await _find_device(stop)
            if device is None:
                break
            try:
                await _serve(mqttc, device, cmd_queue, stop)
            except Exception as e:
                log(f"BLE session error: {e}")
            mqttc.publish(TOPIC_ONLINE, "offline", qos=1, retain=True)
            _drain(cmd_queue)  # don't replay stale commands on reconnect
            if not stop.is_set():
                await asyncio.sleep(RECONNECT_BACKOFF_S)
    finally:
        mqttc.publish(TOPIC_ONLINE, "offline", qos=1, retain=True)
        mqttc.loop_stop()
        mqttc.disconnect()


def main():
    broker = os.environ.get("BUSH_MQTT_BROKER") or get_mqtt_broker()
    log(f"MQTT broker: {broker}:{MQTT_PORT}; BLE target name={BLE_NAME}")

    mqttc = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
    mqttc.will_set(TOPIC_ONLINE, "offline", qos=1, retain=True)

    def on_connect(client, userdata, flags, reason_code, properties):
        log(f"MQTT connected (rc={reason_code})")
        for t in COMMAND_TOPICS:
            client.subscribe(t)
        log(f"subscribed to {len(COMMAND_TOPICS)} valve command topics")

    mqttc.on_connect = on_connect

    try:
        asyncio.run(_run(mqttc, broker))
    except KeyboardInterrupt:
        pass
    log("stopped")


if __name__ == "__main__":
    main()

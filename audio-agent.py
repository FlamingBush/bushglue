#!/usr/bin/env python3
"""
Audio device discovery agent.
Enumerates ALSA devices and publishes the list to bush/audio/devices (retained).
Responds to bush/audio/discover requests at runtime.
"""
import json
import signal
import sys

import paho.mqtt.client as mqtt
import sounddevice as sd

from bushutil import get_mqtt_broker

MQTT_PORT = 1883
TOPIC_DISCOVER = "bush/audio/discover"
TOPIC_DEVICES  = "bush/audio/devices"


def log(msg: str):
    print(f"[audio-agent] {msg}", flush=True)


def _device_list():
    """Return {capture: [...], playback: [...]} from sounddevice.

    Re-initialises PortAudio each time so hot-plugged USB devices are picked up
    (PortAudio caches the device list at initialisation and won't see devices
    that were connected after the process started otherwise).
    """
    sd._terminate()
    sd._initialize()
    capture = []
    playback = []
    for dev in sd.query_devices():
        entry = {
            "index":    dev["index"],
            "name":     dev["name"],
            "channels": max(dev["max_input_channels"], dev["max_output_channels"]),
            "sr":       dev["default_samplerate"],
        }
        if dev["max_input_channels"] > 0:
            entry["channels"] = dev["max_input_channels"]
            capture.append(dict(entry))
        if dev["max_output_channels"] > 0:
            entry["channels"] = dev["max_output_channels"]
            playback.append(dict(entry))
    return {"capture": capture, "playback": playback}


def _publish_devices(client):
    devices = _device_list()
    payload = json.dumps(devices)
    client.publish(TOPIC_DEVICES, payload, retain=True)
    log(f"Published {len(devices['capture'])} capture / {len(devices['playback'])} playback devices")


def on_connect(client, userdata, flags, reason_code, properties):
    log(f"MQTT connected (rc={reason_code})")
    client.subscribe(TOPIC_DISCOVER)
    _publish_devices(client)


def on_message(client, userdata, msg):
    if msg.topic == TOPIC_DISCOVER:
        log("Discover request received")
        _publish_devices(client)


def main():
    broker = get_mqtt_broker()
    log(f"MQTT broker: {broker}:{MQTT_PORT}")

    mqttc = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
    mqttc.on_connect = on_connect
    mqttc.on_message = on_message

    def _shutdown(signum, frame):
        log("Shutting down...")
        mqttc.loop_stop()
        mqttc.disconnect()
        sys.exit(0)

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    try:
        mqttc.connect(broker, MQTT_PORT, 60)
    except Exception as e:
        log(f"Cannot connect to broker: {e}")
        sys.exit(1)

    mqttc.loop_forever()


if __name__ == "__main__":
    main()

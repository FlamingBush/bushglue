#!/usr/bin/env python3
"""
Audio device discovery agent.
Enumerates PulseAudio sources and sinks via pactl and publishes the list to
bush/audio/devices (retained).
Responds to bush/audio/discover requests at runtime.
"""
import json
import re
import signal
import subprocess
import sys

import paho.mqtt.client as mqtt

from bushutil import get_mqtt_broker

MQTT_PORT = 1883
TOPIC_DISCOVER = "bush/audio/discover"
TOPIC_DEVICES  = "bush/audio/devices"


def log(msg: str):
    print(f"[audio-agent] {msg}", flush=True)


def _pactl_list(kind: str) -> list[dict]:
    """Return a list of PA sources or sinks parsed from 'pactl list short <kind>'."""
    try:
        result = subprocess.run(
            ["pactl", "list", "short", kind],
            capture_output=True, text=True, timeout=10,
        )
        entries = []
        for line in result.stdout.splitlines():
            parts = line.split("\t")
            if len(parts) < 2:
                continue
            entry: dict = {"index": int(parts[0]), "name": parts[1]}
            # parts[3] looks like "s16le 2ch 44100Hz" — parse if present
            if len(parts) >= 4:
                fmt = parts[3]
                for seg in fmt.split():
                    if seg.endswith("Hz"):
                        try:
                            entry["sr"] = int(seg[:-2])
                        except ValueError:
                            pass
                    elif seg.endswith("ch"):
                        try:
                            entry["channels"] = int(seg[:-2])
                        except ValueError:
                            pass
            entries.append(entry)
        return entries
    except Exception as e:
        log(f"pactl {kind} error: {e}")
        return []


def _alsa_list(kind: str) -> list[dict]:
    """Return ALSA capture/playback cards from arecord/aplay -l, tagged type='alsa'."""
    cmd = ["arecord", "-l"] if kind == "sources" else ["aplay", "-l"]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=5)
        entries = []
        for line in result.stdout.splitlines():
            m = re.match(r"card (\d+): (\S+) \[([^\]]+)\]", line)
            if m:
                idx, short_name, desc = int(m.group(1)), m.group(2), m.group(3)
                entries.append({
                    "index": idx,
                    "name": f"hw:{short_name}",
                    "description": desc,
                    "type": "alsa",
                })
        return entries
    except Exception as e:
        log(f"alsa {kind} error: {e}")
        return []


def _device_list() -> dict:
    return {
        "capture":  _pactl_list("sources") + _alsa_list("sources"),
        "playback": _pactl_list("sinks")   + _alsa_list("sinks"),
    }


def _publish_devices(client):
    devices = _device_list()
    payload = json.dumps(devices)
    client.publish(TOPIC_DEVICES, payload, retain=True)
    log(f"Published {len(devices['capture'])} sources / {len(devices['playback'])} sinks")


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

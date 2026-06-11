#!/usr/bin/env python3
"""
Audio device discovery agent.
Enumerates PulseAudio sources and sinks via pactl and publishes the list to
bush/audio/devices (retained).
Responds to bush/audio/discover requests at runtime.
"""
import json
import re
import subprocess

from bushutil import make_logger, run_mqtt_service

TOPIC_DISCOVER = "bush/audio/discover"
TOPIC_DEVICES  = "bush/audio/devices"

log = make_logger("audio-agent")


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


def on_message(client, userdata, msg):
    if msg.topic == TOPIC_DISCOVER:
        log("Discover request received")
        _publish_devices(client)


def main():
    run_mqtt_service("audio-agent", [TOPIC_DISCOVER], on_message,
                     on_connect=_publish_devices)


if __name__ == "__main__":
    main()

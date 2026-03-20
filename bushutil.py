"""Shared utilities for Bush Glue service scripts."""
import json
import pathlib
import subprocess

# Persisted audio device selections
_CONFIG_FILE = pathlib.Path.home() / ".config" / "bush" / "audio-devices.json"


def load_audio_device(key: str):
    """Return saved device for *key* ('stt' or 'tts'), or None if not saved."""
    try:
        return json.loads(_CONFIG_FILE.read_text()).get(key)
    except Exception:
        return None


def save_audio_device(key: str, value) -> None:
    """Persist *value* for *key* to the shared audio-devices config file."""
    try:
        _CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
        try:
            data = json.loads(_CONFIG_FILE.read_text())
        except Exception:
            data = {}
        data[key] = value
        tmp = _CONFIG_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(data))
        tmp.replace(_CONFIG_FILE)
    except Exception as e:
        print(f"[bushutil] Failed to save audio device config: {e}", flush=True)


def get_mqtt_broker() -> str:
    """Return the MQTT broker host.

    Under WSL2 the broker runs on the Windows host; detect this via
    /proc/version and resolve the gateway IP.  On native Linux return
    localhost.
    """
    try:
        with open("/proc/version") as f:
            if "microsoft" not in f.read().lower():
                return "localhost"
    except OSError:
        return "localhost"
    result = subprocess.run(["ip", "route", "show"], capture_output=True, text=True)
    for line in result.stdout.splitlines():
        if line.startswith("default"):
            return line.split()[2]
    return "localhost"

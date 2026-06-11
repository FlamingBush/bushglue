"""Shared utilities for Bush Glue service scripts."""
import json
import pathlib
import signal
import subprocess
import sys
import threading

import paho.mqtt.client as mqtt

# Persisted audio device selections
_CONFIG_FILE = pathlib.Path.home() / ".config" / "bush" / "audio-devices.json"

# Persisted general settings (tts_clarity, etc.)
_SETTINGS_FILE = pathlib.Path.home() / ".config" / "bush" / "settings.json"


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


def load_setting(key: str, default=None):
    """Return saved setting for *key*, or *default* if not found."""
    try:
        return json.loads(_SETTINGS_FILE.read_text()).get(key, default)
    except Exception:
        return default


def save_setting(key: str, value) -> None:
    """Persist *value* for *key* to the shared settings file."""
    try:
        _SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
        try:
            data = json.loads(_SETTINGS_FILE.read_text())
        except Exception:
            data = {}
        data[key] = value
        tmp = _SETTINGS_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(data))
        tmp.replace(_SETTINGS_FILE)
    except Exception as e:
        print(f"[bushutil] Failed to save setting: {e}", flush=True)


def build_sox_effects(clarity: int = 0) -> list:
    """Return the sox effects chain for a given clarity level (0-100).

    Clarity 0  = dramatic voice-of-God reverb (default).
    Clarity 100 = most intelligible, still clearly reverbed.

    Interpolated (linear):
      reverberance  65 -> 25   (shorter tail, less consonant masking)
      wet-gain      +3 -> -6 dB (dry signal dominates)
      pre-delay     28 -> 40 ms (more temporal separation)
      room-scale   100 -> 60%  (smaller virtual room, less inter-word smear)

    Fixed (preserve desert character):
      gain -8, pitch -250, HF-damping 12%, stereo-depth 100%

    Always appended:
      compand 0.01,0.05 -70,-70,-30,-15,0,-6 3
        raises quiet consonants relative to loud vowels
    """
    t = max(0, min(100, clarity)) / 100.0
    reverberance = round(65 + t * (25 - 65))   # 65 -> 25
    wet_gain     = round(3  + t * (-6 - 3))     # 3 -> -6
    pre_delay    = round(28 + t * (40 - 28))    # 28 -> 40
    room_scale   = round(100 + t * (60 - 100))  # 100 -> 60
    return [
        "gain", "-8",
        "pitch", "-250",
        "reverb", str(reverberance), "12", str(room_scale), "100", str(pre_delay), str(wet_gain),
        "compand", "0.01,0.05", "-70,-70,-30,-15,0,-6", "3",
    ]


def make_logger(name: str):
    """Return a logger: log("msg") prints "[name] msg", flushed."""
    def log(msg: str):
        print(f"[{name}] {msg}", flush=True)
    return log


def run_mqtt_service(name: str, topics: list, on_message, *, on_connect=None,
                     background_loop=None, on_shutdown=None, port: int = 1883):
    """Shared MQTT service lifecycle; blocks in loop_forever().

    Subscribes to *topics* on (re)connect, then calls on_connect(client) for
    extras like retained-status publishes. SIGTERM/SIGINT run on_shutdown()
    and exit. background_loop(client, stop_event) runs in a daemon thread.
    Must be called from the main thread (signal handlers).
    """
    log = make_logger(name)
    broker = get_mqtt_broker()
    log(f"MQTT broker: {broker}:{port}")

    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
    stop = threading.Event()

    def _on_connect(client, userdata, flags, reason_code, properties):
        log(f"MQTT connected (rc={reason_code})")
        for topic in topics:
            client.subscribe(topic)
        log("Subscribed to " + ", ".join(topics))
        if on_connect:
            on_connect(client)

    client.on_connect = _on_connect
    client.on_message = on_message

    def _shutdown(signum, frame):
        log("Shutting down...")
        stop.set()
        if on_shutdown:
            on_shutdown()
        client.loop_stop()
        client.disconnect()
        sys.exit(0)

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    try:
        client.connect(broker, port, 60)
    except Exception as e:
        log(f"Cannot connect to broker: {e}")
        sys.exit(1)

    if background_loop:
        threading.Thread(target=background_loop, args=(client, stop), daemon=True).start()

    client.loop_forever()


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

"""Shared utilities for Bush Glue service scripts."""
import json
import os
import pathlib
import subprocess

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
    """Return the sox effects chain for a given clarity level (0–100).

    Clarity 0  = dramatic voice-of-God reverb (default).
    Clarity 100 = most intelligible, still clearly reverbed.

    Interpolated (linear):
      reverberance  65 → 25   (shorter tail, less consonant masking)
      wet-gain      +3 → -6 dB (dry signal dominates)
      pre-delay     28 → 40 ms (more temporal separation)
      room-scale   100 → 60%  (smaller virtual room, less inter-word smear)

    Fixed (preserve desert character):
      gain -8, pitch -250, HF-damping 12%, stereo-depth 100%

    Always appended:
      compand 0.01,0.05 -70,-70,-30,-15,0,-6 3
        raises quiet consonants relative to loud vowels
    """
    # Original dramatic settings:
    # gain -8  pitch -250  reverb 65 12 100 100 28 3
    t = max(0, min(100, clarity)) / 100.0
    reverberance = round(65 + t * (25 - 65))   # 65 → 25
    wet_gain     = round(3  + t * (-6 - 3))     # 3 → -6
    pre_delay    = round(28 + t * (40 - 28))    # 28 → 40
    room_scale   = round(100 + t * (60 - 100))  # 100 → 60
    return [
        "gain", "-8",
        "pitch", "-250",
        "reverb", str(reverberance), "12", str(room_scale), "100", str(pre_delay), str(wet_gain),
        "compand", "0.01,0.05", "-70,-70,-30,-15,0,-6", "3",
    ]


def get_mqtt_broker() -> str:
    """Return the MQTT broker host.

    Resolution order:
    1. ``MQTT_BROKER`` environment variable — always wins if set.
       Use this on any native Linux host (ODROID, Raspberry Pi) where the
       broker is on the LAN but not at the gateway address:
           export MQTT_BROKER=192.168.1.42
    2. WSL2 auto-detection — reads ``/proc/version``; if "microsoft" is
       present, resolves the default gateway IP (Windows host).
    3. Fallback: ``localhost``.

    See INSTALL.md § "Deployment targets" for the full topology table.
    """
    env = os.environ.get("MQTT_BROKER")
    if env:
        return env
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

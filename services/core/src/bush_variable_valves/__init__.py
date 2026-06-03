#!/usr/bin/env python3
"""
Flame expression service — maps sentiment + speech state to needle valve position.

Subscribes to:
  bush/pipeline/sentiment/result  — emotion classification from DistilBERT
  bush/pipeline/tts/speaking      — utterance start
  bush/pipeline/tts/done          — utterance end
  bush/fire/valve/status          — Pico valve state (logged on transitions only)
  bush/fire/valve/auto            — on/off; when off, suspend target output so a
                                    manual position (e.g. from bush-monitor) holds

Publishes to:
  bush/fire/valve/target           — float 0.0 (closed) to 1.0 (open), 10 Hz

The valve target tracks the bush's emotional intent:
  - Baseline position is driven by sentiment (valence mapping)
  - Modulated by speech state (rise on speak, drop on silence)
  - Slow LFO "breathing" when sentiment is static
  - Smooth ramping between sentiment changes
"""

import json
import signal
import sys
import threading
import time

import paho.mqtt.client as mqtt

from bushutil import get_mqtt_broker

# ── MQTT topics ─────────────────────────────────────────────────────────────
TOPIC_SENTIMENT  = "bush/pipeline/sentiment/result"
TOPIC_SPEAKING   = "bush/pipeline/tts/speaking"
TOPIC_DONE       = "bush/pipeline/tts/done"
TOPIC_VALVE_TARGET = "bush/fire/valve/target"
TOPIC_VALVE_STATUS = "bush/fire/valve/status"
TOPIC_VALVE_AUTO   = "bush/fire/valve/auto"
MQTT_PORT = 1883

# Pico state values that are part of normal operation. Transitions WITHIN this
# set are skipped to avoid logging every idle↔moving cycle (~2 Hz). Transitions
# into/out of any other state (error, stalled, homing, homing_finalize) get
# logged. last_error changes are always logged.
_ROUTINE_VALVE_STATES = {"idle", "moving"}

# ── Sentiment → baseline mapping ───────────────────────────────────────────
# Each emotion maps to a baseline valve position (0.0=closed, 1.0=open).
# These values represent the bush's "resting flame" for each emotional state.
EMOTION_BASELINES = {
    "anger":    0.85,   # large aggressive flame
    "joy":      0.70,   # lively, bouncy
    "love":     0.60,   # warm, sustained
    "surprise": 0.75,   # startled high
    "fear":     0.25,   # trembling low
    "sadness":  0.15,   # mournful, barely alive
}
DEFAULT_BASELINE = 0.35  # visible but modest — fallback / no sentiment

# ── Modulation parameters ──────────────────────────────────────────────────
# Breathing (sinusoidal LFO) lives in firmware (bush/fire/valve/breath); this
# service emits only the sentiment baseline + speech modulation.

# Speech modulation
SPEECH_RISE    = 0.10     # +10% at utterance start
SPEECH_DROP    = 0.12     # -12% during silence > SILENCE_THRESHOLD_S
SPEECH_RISE_S  = 0.2      # ramp up over 200ms
SPEECH_DECAY_S = 0.8      # decay back over 800ms
SILENCE_THRESHOLD_S = 0.4 # silence must exceed this before dropping

# Baseline ramping
RAMP_DURATION_S = 1.5     # ramp between sentiment changes over 1.5s

# Publish rate
PUBLISH_HZ = 2
PUBLISH_INTERVAL_S = 1.0 / PUBLISH_HZ

# Stale sentiment timeout — revert to default if no sentiment for this long
STALE_TIMEOUT_S = 60.0


def log(msg: str):
    print(f"[variable-valves] {msg}", flush=True)


# ── State ───────────────────────────────────────────────────────────────────
_lock = threading.Lock()

_baseline_current = DEFAULT_BASELINE  # current (ramped) baseline
_baseline_target  = DEFAULT_BASELINE  # target baseline from latest sentiment
_baseline_start   = 0.0               # time when ramp started
_baseline_from    = DEFAULT_BASELINE  # baseline value at ramp start

_speaking = False
_speech_start_time = 0.0
_speech_end_time   = 0.0
_last_sentiment_time = 0.0

_confidence = 0.5  # top emotion confidence score

_valve_last_state = None
_valve_last_error = None

# When False, suspend target publishing so a manual position holds. Toggled via
# bush/fire/valve/auto (retained, so it survives restarts and late subscribers).
_auto_enabled = True


def _parse_auto(payload) -> "bool | None":
    """Tolerant on/off parser. Accepts on/off/true/false/1/0/yes/no and
    JSON {"enabled": bool}. Returns None on garbage."""
    if isinstance(payload, (bytes, bytearray)):
        payload = payload.decode(errors="ignore")
    s = str(payload).strip().lower()
    if s in ("on", "true", "1", "yes"):
        return True
    if s in ("off", "false", "0", "no"):
        return False
    try:
        data = json.loads(payload)
        if isinstance(data, dict) and "enabled" in data:
            return bool(data["enabled"])
    except (ValueError, TypeError):
        pass
    return None


def _on_auto(payload: bytes):
    global _auto_enabled
    val = _parse_auto(payload)
    if val is None:
        log(f"auto: bad payload: {payload!r}")
        return
    with _lock:
        changed = (val != _auto_enabled)
        _auto_enabled = val
    if changed:
        log("auto on — resuming target output" if val
            else "auto off — suspending target output (manual control)")


def _on_sentiment(payload: bytes):
    global _baseline_target, _baseline_from, _baseline_start
    global _last_sentiment_time, _confidence
    try:
        data = json.loads(payload)
        classification = data.get("classification", [])
        if not classification:
            return
        # classification is a list of {label, score} sorted by confidence
        top = max(classification, key=lambda x: x["score"])
        label = top["label"]
        score = top["score"]

        new_baseline = EMOTION_BASELINES.get(label, DEFAULT_BASELINE)
        # Scale toward default when confidence is low
        new_baseline = DEFAULT_BASELINE + (new_baseline - DEFAULT_BASELINE) * score

        with _lock:
            _baseline_from = _baseline_current
            _baseline_target = new_baseline
            _baseline_start = time.monotonic()
            _last_sentiment_time = time.monotonic()
            _confidence = score

        log(f"sentiment: {label} ({score:.2f}) -> baseline {new_baseline:.2f}")
    except Exception as e:
        log(f"sentiment parse error: {e}")


def _on_speaking(payload: bytes):
    global _speaking, _speech_start_time
    with _lock:
        _speaking = True
        _speech_start_time = time.monotonic()
    log("speaking started")


def _on_valve_status(payload: bytes):
    global _valve_last_state, _valve_last_error
    try:
        data = json.loads(payload)
    except Exception:
        return
    state = data.get("state")
    err = data.get("last_error")

    if state != _valve_last_state:
        if state not in _ROUTINE_VALVE_STATES or _valve_last_state not in _ROUTINE_VALVE_STATES:
            log(f"valve state: {_valve_last_state} -> {state}")
        _valve_last_state = state

    if err != _valve_last_error:
        if err is not None:
            log(f"valve error: {err} (state={state}, pos={data.get('pos')}, target={data.get('target')})")
        else:
            log(f"valve error cleared (was {_valve_last_error})")
        _valve_last_error = err


def _on_done(payload: bytes):
    global _speaking, _speech_end_time
    with _lock:
        _speaking = False
        _speech_end_time = time.monotonic()
    log("speaking done")


def _compute_target() -> float:
    """Compute the current valve target. Called at PUBLISH_HZ."""
    now = time.monotonic()

    with _lock:
        # Ramp baseline toward target
        elapsed = now - _baseline_start
        if elapsed >= RAMP_DURATION_S:
            baseline = _baseline_target
        else:
            t = elapsed / RAMP_DURATION_S
            # Smooth ease-in-out
            t = t * t * (3 - 2 * t)
            baseline = _baseline_from + (_baseline_target - _baseline_from) * t

        # Update current baseline for next ramp
        global _baseline_current
        _baseline_current = baseline

        # Check for stale sentiment
        if _last_sentiment_time > 0 and (now - _last_sentiment_time) > STALE_TIMEOUT_S:
            baseline = DEFAULT_BASELINE

        speaking = _speaking
        speech_start = _speech_start_time
        speech_end = _speech_end_time

    target = baseline

    # Speech modulation
    if speaking:
        # Rise at utterance start, then decay toward baseline
        since_start = now - speech_start
        if since_start < SPEECH_RISE_S:
            # Ramp up
            rise_frac = since_start / SPEECH_RISE_S
            target += SPEECH_RISE * rise_frac
        elif since_start < SPEECH_RISE_S + SPEECH_DECAY_S:
            # Decay back
            decay_frac = (since_start - SPEECH_RISE_S) / SPEECH_DECAY_S
            target += SPEECH_RISE * (1 - decay_frac)
        # else: back at baseline
    else:
        # Not speaking — check silence duration
        if speech_end > 0:
            silence_duration = now - speech_end
            if silence_duration > SILENCE_THRESHOLD_S:
                # Drop below baseline, capped at SPEECH_DROP
                drop_frac = min(1.0, (silence_duration - SILENCE_THRESHOLD_S) / SPEECH_DECAY_S)
                target -= SPEECH_DROP * drop_frac

    # Clamp to valid range
    return max(0.0, min(1.0, target))


def _publish_loop(mqttc: mqtt.Client, stop: threading.Event):
    """Publish valve target at PUBLISH_HZ until stopped. While auto is disabled
    (bush/fire/valve/auto = off) keep computing — so the baseline ramp stays
    coherent and resumes smoothly — but suppress output so a manual target holds."""
    while not stop.is_set():
        target = _compute_target()
        with _lock:
            enabled = _auto_enabled
        if enabled:
            try:
                mqttc.publish(TOPIC_VALVE_TARGET, f"{target:.3f}")
            except Exception as e:
                log(f"publish error: {e}")
        stop.wait(PUBLISH_INTERVAL_S)


def main():
    broker = get_mqtt_broker()
    log(f"MQTT broker: {broker}:{MQTT_PORT}")

    mqttc = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
    stop = threading.Event()

    def on_connect(client, userdata, flags, reason_code, properties):
        log(f"MQTT connected (rc={reason_code})")
        client.subscribe(TOPIC_SENTIMENT)
        client.subscribe(TOPIC_SPEAKING)
        client.subscribe(TOPIC_DONE)
        client.subscribe(TOPIC_VALVE_STATUS)
        client.subscribe(TOPIC_VALVE_AUTO)
        log(f"Subscribed to sentiment, tts/speaking, tts/done, valve/status, valve/auto")

    def on_message(client, userdata, msg):
        if msg.topic == TOPIC_SENTIMENT:
            _on_sentiment(msg.payload)
        elif msg.topic == TOPIC_SPEAKING:
            _on_speaking(msg.payload)
        elif msg.topic == TOPIC_DONE:
            _on_done(msg.payload)
        elif msg.topic == TOPIC_VALVE_STATUS:
            _on_valve_status(msg.payload)
        elif msg.topic == TOPIC_VALVE_AUTO:
            _on_auto(msg.payload)

    mqttc.on_connect = on_connect
    mqttc.on_message = on_message

    def _shutdown(signum, frame):
        log("Shutting down...")
        stop.set()
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

    publisher = threading.Thread(target=_publish_loop, args=(mqttc, stop), daemon=True)
    publisher.start()

    mqttc.loop_forever()


if __name__ == "__main__":
    main()

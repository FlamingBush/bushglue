#!/usr/bin/env python3
"""
Text-to-speech service for Bush Glue.
Subscribes to bush/pipeline/t2v/verse and speaks each verse aloud via espeak-ng.
Queues verses so rapid-fire messages don't overlap; drops stale items if the
queue backs up so playback stays roughly in sync with the pipeline.

Accepts runtime output device changes via bush/audio/tts/set-device {"device": <str|null>}.
"""
import json
import queue
import signal
import subprocess
import sys
import threading
import time

import paho.mqtt.client as mqtt

from bushutil import (get_mqtt_broker, load_audio_device, save_audio_device,
                      load_setting, save_setting, build_sox_effects)

# ── config ─────────────────────────────────────────────────────────────────
TOPIC_VERSE         = "bush/pipeline/t2v/verse"
TOPIC_SPEAKING      = "bush/pipeline/tts/speaking"
TOPIC_DONE          = "bush/pipeline/tts/done"
TOPIC_SET_DEVICE    = "bush/audio/tts/set-device"
TOPIC_DEVICE_STATUS = "bush/audio/tts/device"
TOPIC_SET_CLARITY   = "bush/audio/tts/set-clarity"
TOPIC_CLARITY       = "bush/audio/tts/clarity"
MQTT_PORT = 1883

# Extra silence after sox finishes before signalling done (reverb tail)
DONE_TAIL_S = 0.5

# Failsafe: kill sox if it hasn't finished within this many seconds
TTS_TIMEOUT_S = 60

# espeak-ng → sox pipeline for the voice of God:
#   en-gb:  British RP — more gravitas than en-us
#   -s 95:  slow and deliberate
#   -p 1:   minimum pitch (espeak range 0-99)
#   -a 200: maximum amplitude out of espeak
ESPEAK_CMD = ["espeak-ng", "-v", "en-gb", "-s", "95", "-p", "1", "-a", "200", "--stdout"]

# sox effects applied after espeak (clarity=0 defaults):
#   gain -8        headroom before effects to prevent clipping
#   pitch -250     ~2.5 semitones down — deep but not subterranean
#   reverb 65 12 100 100 28 3
#     65%  reverberance  — long open tail, not dense
#     12%  HF-damping    — stay bright; rock/sky reflect high freqs well
#     100% room-scale    — vast open space
#     100% stereo-depth  — wide horizon
#     28ms pre-delay     — sound crossing distance before cliff echo returns
#     3dB  wet-gain      — present but not drowning the voice
# SOX_EFFECTS = ["gain", "-8", "pitch", "-250", "reverb", "65", "12", "100", "100", "28", "3"]
# (now generated dynamically via build_sox_effects(_tts_clarity))

# Drop queued verses beyond this depth so we never fall minutes behind
QUEUE_MAX = 2

# Current TTS output device: None → sox default (-d), str → ALSA device name
_tts_device: str | None = load_audio_device("tts")  # restore last saved device (or None)
_device_lock = threading.Lock()

# Current clarity level (0 = dramatic/default, 100 = most intelligible)
_tts_clarity: int = load_setting("tts_clarity", 0)
_clarity_lock = threading.Lock()


def _sox_cmd() -> list[str]:
    """Build the sox command using the current output device and clarity."""
    with _device_lock:
        dev = _tts_device
    with _clarity_lock:
        clarity = _tts_clarity
    if dev is None:
        output_args = ["-d"]
    else:
        output_args = ["-t", "alsa", dev]
    return ["sox", "-q", "-t", "wav", "-"] + output_args + build_sox_effects(clarity)


def log(msg: str):
    print(f"[tts-service] {msg}", flush=True)


# ── speech worker ───────────────────────────────────────────────────────────
speech_queue: queue.Queue[str | None] = queue.Queue(maxsize=QUEUE_MAX)
_current_procs: list[subprocess.Popen] = []
_proc_lock = threading.Lock()
_mqttc: mqtt.Client | None = None   # set after connect


def _kill_current():
    """Kill any in-progress espeak+sox processes immediately."""
    with _proc_lock:
        for p in _current_procs:
            if p.poll() is None:
                p.kill()
        _current_procs.clear()



def _speak_worker():
    """Runs in a background thread; pulls verses and speaks them one at a time."""
    while True:
        text = speech_queue.get()
        if text is None:
            break
        log(f"Speaking: {text[:80]!r}")
        if _mqttc:
            try:
                _mqttc.publish(TOPIC_SPEAKING, json.dumps({"text": text, "ts": time.time()}))
            except Exception:
                pass
        try:
            espeak = subprocess.Popen(
                ESPEAK_CMD + [text],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
            )
            sox = subprocess.Popen(
                _sox_cmd(),
                stdin=espeak.stdout,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
            )
            espeak.stdout.close()
            with _proc_lock:
                _current_procs.extend([espeak, sox])
            timed_out = False
            try:
                sox.wait(timeout=TTS_TIMEOUT_S)
            except subprocess.TimeoutExpired:
                log(f"sox timed out after {TTS_TIMEOUT_S}s — killing")
                timed_out = True
                sox.kill()
                sox.wait()
            try:
                espeak.wait(timeout=5)
            except subprocess.TimeoutExpired:
                log("espeak timed out — killing")
                espeak.kill()
                espeak.wait()
            sox_rc = sox.returncode
            sox_err = (sox.stderr.read().decode(errors="replace").strip()
                       if sox.stderr else "")
            if sox_err and "can't encode 0-bit" not in sox_err:
                log(f"sox stderr (rc={sox_rc}): {sox_err}")
            # rc=-9: killed via _kill_current (SIGKILL) or timeout — still publish done
            # rc=other non-zero: sox device/format error — skip done to avoid false gate clear
            was_killed = sox_rc == -9 and not timed_out
            sox_failed = sox_rc not in (0, None) and not was_killed and not timed_out
            if timed_out:
                log(f"sox timed out (rc={sox_rc}) — publishing done to unblock pipeline")
            elif sox_failed:
                log(f"sox failed (rc={sox_rc}) — skipping done signal")
            with _proc_lock:
                _current_procs.clear()
            if not was_killed and not timed_out and not sox_failed:
                time.sleep(DONE_TAIL_S)
            if _mqttc and not sox_failed:
                try:
                    _mqttc.publish(TOPIC_DONE, json.dumps({"ts": time.time()}))
                except Exception:
                    pass
        except Exception as e:
            log(f"speak error: {e}")
        speech_queue.task_done()


def _enqueue(text: str):
    """Add verse to queue, dropping oldest if full."""
    try:
        speech_queue.put_nowait(text)
    except queue.Full:
        try:
            dropped = speech_queue.get_nowait()
            log(f"Queue full — dropped: {dropped[:40]!r}")
            speech_queue.task_done()
        except queue.Empty:
            pass
        try:
            speech_queue.put_nowait(text)
        except queue.Full:
            log("Queue still full, skipping verse.")


def _interrupt_and_enqueue(text: str):
    """Interrupt current speech and drain queue before enqueuing new verse."""
    _kill_current()
    while not speech_queue.empty():
        try:
            speech_queue.get_nowait()
            speech_queue.task_done()
        except queue.Empty:
            break
    _enqueue(text)


# ── MQTT ────────────────────────────────────────────────────────────────────
def on_connect(client, userdata, flags, reason_code, properties):
    log(f"MQTT connected (rc={reason_code})")
    client.subscribe(TOPIC_VERSE)
    client.subscribe(TOPIC_SET_DEVICE)
    client.subscribe(TOPIC_SET_CLARITY)
    log(f"Subscribed to {TOPIC_VERSE}")
    with _device_lock:
        dev = _tts_device
    client.publish(TOPIC_DEVICE_STATUS, json.dumps({"device": dev}), retain=True)
    with _clarity_lock:
        clarity = _tts_clarity
    client.publish(TOPIC_CLARITY, json.dumps({"clarity": clarity}), retain=True)


def on_message(client, userdata, msg):
    global _tts_device, _tts_clarity
    if msg.topic == TOPIC_VERSE:
        try:
            data = json.loads(msg.payload)
            text = data.get("text", "").strip()
            if not text:
                return
            first_para = text.split("\n\n")[0]
            clean = " ".join(line.strip() for line in first_para.splitlines() if line.strip())
            _interrupt_and_enqueue(clean)
        except Exception as e:
            log(f"Message error: {e}")
    elif msg.topic == TOPIC_SET_DEVICE:
        try:
            data = json.loads(msg.payload)
            raw = data.get("device")   # None or str
            dev = str(raw) if raw is not None else None
            with _device_lock:
                _tts_device = dev
            save_audio_device("tts", dev)
            log(f"Output device set to: {dev!r}")
            client.publish(TOPIC_DEVICE_STATUS,
                           json.dumps({"device": dev, "status": "ok"}), retain=True)
        except Exception as e:
            log(f"set-device error: {e}")
    elif msg.topic == TOPIC_SET_CLARITY:
        try:
            data = json.loads(msg.payload)
            raw = int(data.get("clarity", 0))
            clamped = max(0, min(100, raw))
            with _clarity_lock:
                _tts_clarity = clamped
            save_setting("tts_clarity", clamped)
            log(f"Clarity set to: {clamped}")
            client.publish(TOPIC_CLARITY,
                           json.dumps({"clarity": clamped, "status": "ok"}), retain=True)
        except Exception as e:
            log(f"set-clarity error: {e}")


def main():
    broker = get_mqtt_broker()
    log(f"MQTT broker: {broker}:{MQTT_PORT}")

    worker = threading.Thread(target=_speak_worker, daemon=True)
    worker.start()

    global _mqttc
    mqttc = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
    mqttc.on_connect = on_connect
    mqttc.on_message = on_message
    _mqttc = mqttc

    def _shutdown(signum, frame):
        log("Shutting down...")
        speech_queue.put(None)
        _kill_current()
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

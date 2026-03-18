#!/usr/bin/env python3
"""
Text-to-speech service for Bush Glue.
Subscribes to bush/pipeline/t2v/verse and speaks each verse aloud via espeak-ng.
Queues verses so rapid-fire messages don't overlap; drops stale items if the
queue backs up so playback stays roughly in sync with the pipeline.
"""
import json
import queue
import signal
import subprocess
import sys
import threading
import time

import paho.mqtt.client as mqtt

# ── config ─────────────────────────────────────────────────────────────────
TOPIC_VERSE = "bush/pipeline/t2v/verse"
TOPIC_SPEAKING = "bush/pipeline/tts/speaking"
MQTT_PORT = 1883

ESPEAK = "espeak-ng"
# Slightly slower rate and a warmer voice for scripture-reading feel
ESPEAK_ARGS = ["-v", "en-us", "-s", "140", "-p", "40"]

# Drop queued verses beyond this depth so we never fall minutes behind
QUEUE_MAX = 2


def _windows_host_ip() -> str:
    result = subprocess.run(["ip", "route", "show"], capture_output=True, text=True)
    for line in result.stdout.splitlines():
        if line.startswith("default"):
            return line.split()[2]
    return "localhost"


def log(msg: str):
    print(f"[tts-service] {msg}", flush=True)


# ── speech worker ───────────────────────────────────────────────────────────
speech_queue: queue.Queue[str | None] = queue.Queue(maxsize=QUEUE_MAX)
_current_proc: subprocess.Popen | None = None
_proc_lock = threading.Lock()
_mqttc: mqtt.Client | None = None   # set after connect


def _speak_worker():
    """Runs in a background thread; pulls verses and speaks them one at a time."""
    global _current_proc
    while True:
        text = speech_queue.get()
        if text is None:          # shutdown sentinel
            break
        log(f"Speaking: {text[:80]!r}")
        if _mqttc:
            try:
                _mqttc.publish(TOPIC_SPEAKING, json.dumps({"text": text, "ts": time.time()}))
            except Exception:
                pass
        try:
            with _proc_lock:
                _current_proc = subprocess.Popen(
                    [ESPEAK] + ESPEAK_ARGS + [text],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            _current_proc.wait()
            with _proc_lock:
                _current_proc = None
        except Exception as e:
            log(f"espeak error: {e}")
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
    # Kill current playback
    with _proc_lock:
        if _current_proc and _current_proc.poll() is None:
            _current_proc.terminate()

    # Drain stale queue entries
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
    log(f"Subscribed to {TOPIC_VERSE}")


def on_message(client, userdata, msg):
    try:
        data = json.loads(msg.payload)
        text = data.get("text", "").strip()
        if not text:
            return
        # Flatten multi-line verse — remove annotation lines (indented or after \n\n)
        # Keep only the first paragraph (the verse itself, not footnotes)
        first_para = text.split("\n\n")[0]
        clean = " ".join(line.strip() for line in first_para.splitlines() if line.strip())
        _interrupt_and_enqueue(clean)
    except Exception as e:
        log(f"Message error: {e}")


def main():
    broker = _windows_host_ip()
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
        speech_queue.put(None)   # stop worker
        with _proc_lock:
            if _current_proc and _current_proc.poll() is None:
                _current_proc.terminate()
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

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
TOPIC_DONE = "bush/pipeline/tts/done"
MQTT_PORT = 1883

# Extra silence after sox finishes before signalling done (reverb tail)
DONE_TAIL_S = 0.5

# espeak-ng → sox pipeline for the voice of God:
#   en-gb:  British RP — more gravitas than en-us
#   -s 95:  slow and deliberate
#   -p 1:   minimum pitch (espeak range 0-99)
#   -a 200: maximum amplitude out of espeak
ESPEAK_CMD = ["espeak-ng", "-v", "en-gb", "-s", "95", "-p", "1", "-a", "200", "--stdout"]

# sox effects applied after espeak:
#   gain -8        headroom before effects to prevent clipping
#   pitch -250     ~2.5 semitones down — deep but not subterranean
#   reverb 65 12 100 100 28 3
#     65%  reverberance  — long open tail, not dense
#     12%  HF-damping    — stay bright; rock/sky reflect high freqs well
#     100% room-scale    — vast open space
#     100% stereo-depth  — wide horizon
#     28ms pre-delay     — sound crossing distance before cliff echo returns
#     3dB  wet-gain      — present but not drowning the voice
SOX_CMD = ["sox", "-t", "wav", "-", "-d",
           "gain", "-8",
           "pitch", "-250",
           "reverb", "65", "12", "100", "100", "28", "3"]

# Drop queued verses beyond this depth so we never fall minutes behind
QUEUE_MAX = 2


def _windows_host_ip() -> str:
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


def log(msg: str):
    print(f"[tts-service] {msg}", flush=True)


# ── speech worker ───────────────────────────────────────────────────────────
speech_queue: queue.Queue[str | None] = queue.Queue(maxsize=QUEUE_MAX)
# Both espeak and sox processes; killed together on interrupt
_current_procs: list[subprocess.Popen] = []
_proc_lock = threading.Lock()
_mqttc: mqtt.Client | None = None   # set after connect


def _kill_current():
    """Kill any in-progress espeak+sox processes immediately."""
    with _proc_lock:
        for p in _current_procs:
            if p.poll() is None:
                p.kill()   # SIGKILL — exits immediately, no graceful drain
        _current_procs.clear()


def _pa_keepalive():
    """Play silence every 4 s through sox to keep PulseAudio awake.
    Without this, WSLg's PA sink sleeps after a few seconds of silence and
    the first real sox invocation stalls for several seconds while it wakes."""
    # warm up immediately on startup
    time.sleep(0.5)
    while True:
        try:
            subprocess.run(
                ["sox", "-n", "-d", "synth", "0.1", "sin", "0", "vol", "0"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                timeout=3,
            )
        except Exception:
            pass
        time.sleep(4)


def _speak_worker():
    """Runs in a background thread; pulls verses and speaks them one at a time."""
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
            # espeak writes WAV to stdout; sox reads it and plays with effects
            espeak = subprocess.Popen(
                ESPEAK_CMD + [text],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
            )
            sox = subprocess.Popen(
                SOX_CMD,
                stdin=espeak.stdout,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            espeak.stdout.close()   # let sox own the pipe
            with _proc_lock:
                _current_procs.extend([espeak, sox])
            sox.wait()
            espeak.wait()
            was_killed = sox.returncode not in (0, None)
            with _proc_lock:
                _current_procs.clear()
            if not was_killed:
                time.sleep(DONE_TAIL_S)
            if _mqttc:
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

    keepalive = threading.Thread(target=_pa_keepalive, daemon=True)
    keepalive.start()

    global _mqttc
    mqttc = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
    mqttc.on_connect = on_connect
    mqttc.on_message = on_message
    _mqttc = mqttc

    def _shutdown(signum, frame):
        log("Shutting down...")
        speech_queue.put(None)   # stop worker
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

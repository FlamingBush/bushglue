#!/usr/bin/env python3
"""
Speech-to-Text MQTT publisher.
Reads from microphone using Vosk, publishes final transcriptions to
bush/pipeline/stt/transcript as {"text": "...", "ts": <epoch>}.

Mutes itself while TTS is speaking (bush/pipeline/tts/speaking) and
unmutes on bush/pipeline/tts/done, resetting the Vosk recognizer so
any partial state from hearing TTS speech is discarded.
"""
import json
import queue
import subprocess
import sys
import threading
import time

import paho.mqtt.client as mqtt
import sounddevice as sd

# ── paths ──────────────────────────────────────────────────────────────────
STT_DIR = "/mnt/c/Users/EB/speech-to-text"
MODEL_PATH = f"{STT_DIR}/models/en-us"
SAMPLE_RATE = 16000

# ── MQTT ───────────────────────────────────────────────────────────────────
TOPIC_TRANSCRIPT = "bush/pipeline/stt/transcript"
TOPIC_TTS_SPEAKING = "bush/pipeline/tts/speaking"
TOPIC_TTS_DONE = "bush/pipeline/tts/done"
MQTT_PORT = 1883


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
    print(f"[stt-service] {msg}", flush=True)


def main():
    broker = _windows_host_ip()
    log(f"MQTT broker: {broker}:{MQTT_PORT}")

    # ── mute gate ──────────────────────────────────────────────────────────
    # Set while TTS is speaking; audio is drained but not processed.
    muted = threading.Event()
    # Signals the audio loop to recreate the Vosk recognizer after unmuting.
    reset_recognizer = threading.Event()

    def on_tts_speaking():
        if not muted.is_set():
            log("Muting STT (TTS speaking)")
        muted.set()

    def on_tts_done():
        muted.clear()
        reset_recognizer.set()
        log("Unmuting STT (TTS done)")

    # ── MQTT setup ─────────────────────────────────────────────────────────
    def on_message(client, userdata, msg):
        if msg.topic == TOPIC_TTS_SPEAKING:
            on_tts_speaking()
        elif msg.topic == TOPIC_TTS_DONE:
            on_tts_done()

    def on_connect(client, userdata, flags, reason_code, properties):
        client.subscribe(TOPIC_TTS_SPEAKING)
        client.subscribe(TOPIC_TTS_DONE)

    mqttc = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
    mqttc.on_connect = on_connect
    mqttc.on_message = on_message
    mqttc.connect(broker, MQTT_PORT, 60)
    mqttc.loop_start()
    log("MQTT connected.")

    # ── Vosk setup ─────────────────────────────────────────────────────────
    sys.path.insert(0, STT_DIR)
    from transcriber import SpeechToText
    from vosk import KaldiRecognizer

    stt = SpeechToText(model_path=MODEL_PATH, sample_rate=SAMPLE_RATE)
    audio_queue: queue.Queue[bytes] = queue.Queue()

    def callback(indata, frames, time_info, status):
        if status:
            log(f"sounddevice status: {status}")
        audio_queue.put(bytes(indata))

    log(f"Opening microphone at {SAMPLE_RATE} Hz...")
    try:
        with sd.RawInputStream(
            samplerate=SAMPLE_RATE,
            blocksize=8000,
            dtype="int16",
            channels=1,
            callback=callback,
        ):
            log("Listening. Speak a query...")
            while True:
                data = audio_queue.get()

                # Recreate recognizer after unmute to discard TTS-contaminated state
                if reset_recognizer.is_set():
                    reset_recognizer.clear()
                    stt.recognizer = KaldiRecognizer(stt.model, SAMPLE_RATE)
                    log("Recognizer reset.")

                if muted.is_set():
                    # Drain audio silently — don't feed TTS speech to the model
                    continue

                result = stt.accept_audio(data)

                if result["type"] == "final" and result["text"]:
                    text = result["text"]
                    log(f"Final: {text!r}")
                    mqttc.publish(TOPIC_TRANSCRIPT, json.dumps({"text": text, "ts": time.time()}))
                elif result["type"] == "partial" and result["text"]:
                    print(f"\rPartial: {result['text']}", end="", flush=True)

    except KeyboardInterrupt:
        log("Interrupted.")
    finally:
        mqttc.loop_stop()
        mqttc.disconnect()
        log("Done.")


if __name__ == "__main__":
    main()

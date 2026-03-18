#!/usr/bin/env python3
"""
Speech-to-Text MQTT publisher.
Reads from microphone using Vosk, publishes final transcriptions to
bush/pipeline/stt/transcript as {"text": "...", "ts": <epoch>}.
"""
import json
import queue
import subprocess
import sys
import time

import paho.mqtt.client as mqtt
import sounddevice as sd

# ── paths ──────────────────────────────────────────────────────────────────
STT_DIR = "/mnt/c/Users/EB/speech-to-text"
MODEL_PATH = f"{STT_DIR}/models/en-us"
SAMPLE_RATE = 16000

# ── MQTT ───────────────────────────────────────────────────────────────────
TOPIC_TRANSCRIPT = "bush/pipeline/stt/transcript"
MQTT_PORT = 1883


def _windows_host_ip() -> str:
    result = subprocess.run(["ip", "route", "show"], capture_output=True, text=True)
    for line in result.stdout.splitlines():
        if line.startswith("default"):
            return line.split()[2]
    return "localhost"


def log(msg: str):
    print(f"[stt-service] {msg}", flush=True)


def main():
    # Resolve broker IP at startup
    broker = _windows_host_ip()
    log(f"MQTT broker: {broker}:{MQTT_PORT}")

    # Connect MQTT
    mqttc = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
    mqttc.connect(broker, MQTT_PORT, 60)
    mqttc.loop_start()
    log("MQTT connected.")

    # Import SpeechToText after resolving paths
    sys.path.insert(0, STT_DIR)
    from transcriber import SpeechToText  # noqa: E402

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
                result = stt.accept_audio(data)

                if result["type"] == "final" and result["text"]:
                    text = result["text"]
                    log(f"Final: {text!r}")
                    payload = json.dumps({"text": text, "ts": time.time()})
                    mqttc.publish(TOPIC_TRANSCRIPT, payload)
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

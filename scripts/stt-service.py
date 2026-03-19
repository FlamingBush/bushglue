#!/usr/bin/env python3
"""
Speech-to-Text MQTT publisher.
Reads from microphone using Vosk, publishes final transcriptions to
bush/pipeline/stt/transcript as {"text": "...", "ts": <epoch>}.

Mutes itself while TTS is speaking (bush/pipeline/tts/speaking) and
unmutes on bush/pipeline/tts/done, resetting the Vosk recognizer so
any partial state from hearing TTS speech is discarded.

Accepts runtime device changes via bush/audio/stt/set-device {"device": <int|str>}.
"""
import json
import os
import queue
import sys
import threading
import time

import paho.mqtt.client as mqtt
import sounddevice as sd

# ── paths / device ─────────────────────────────────────────────────────────
STT_DIR = os.environ.get("STT_DIR", "/mnt/c/Users/EB/speech-to-text")
MODEL_PATH = os.environ.get("STT_MODEL", f"{STT_DIR}/models/en-us")
_dev = os.environ.get("STT_DEVICE")
STT_DEVICE = int(_dev) if _dev and _dev.isdigit() else _dev  # int index or string name
SAMPLE_RATE = 16000

# ── MQTT ───────────────────────────────────────────────────────────────────
TOPIC_TRANSCRIPT    = "bush/pipeline/stt/transcript"
TOPIC_TTS_SPEAKING  = "bush/pipeline/tts/speaking"
TOPIC_TTS_DONE      = "bush/pipeline/tts/done"
TOPIC_SET_DEVICE    = "bush/audio/stt/set-device"
TOPIC_DEVICE_STATUS = "bush/audio/stt/device"
MQTT_PORT = 1883


from bushutil import mqtt_broker as _windows_host_ip


def log(msg: str):
    print(f"[stt-service] {msg}", flush=True)


def main():
    broker = _windows_host_ip()
    log(f"MQTT broker: {broker}:{MQTT_PORT}")

    # ── mute gate ──────────────────────────────────────────────────────────
    muted = threading.Event()
    reset_recognizer = threading.Event()

    # ── device change ──────────────────────────────────────────────────────
    device_change = threading.Event()
    next_device = [STT_DEVICE]   # list so inner functions can mutate it

    def on_tts_speaking():
        if not muted.is_set():
            log("Muting STT (TTS speaking)")
        muted.set()

    def on_tts_done():
        muted.clear()
        reset_recognizer.set()
        log("Unmuting STT (TTS done)")

    # ── MQTT setup ─────────────────────────────────────────────────────────
    mqttc = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)

    def on_message(client, userdata, msg):
        if msg.topic == TOPIC_TTS_SPEAKING:
            on_tts_speaking()
        elif msg.topic == TOPIC_TTS_DONE:
            on_tts_done()
        elif msg.topic == TOPIC_SET_DEVICE:
            try:
                data = json.loads(msg.payload)
                raw = data.get("device")
                if raw is None:
                    return
                dev = int(raw) if str(raw).lstrip("-").isdigit() else str(raw)
                log(f"Device change requested: {dev!r}")
                next_device[0] = dev
                device_change.set()
            except Exception as e:
                log(f"set-device error: {e}")

    def on_connect(client, userdata, flags, reason_code, properties):
        client.subscribe(TOPIC_TTS_SPEAKING)
        client.subscribe(TOPIC_TTS_DONE)
        client.subscribe(TOPIC_SET_DEVICE)
        # Publish current device on reconnect
        client.publish(TOPIC_DEVICE_STATUS,
                       json.dumps({"device": next_device[0]}), retain=True)

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

    # ── restartable audio loop ─────────────────────────────────────────────
    current_device = STT_DEVICE
    try:
        while True:
            device_change.clear()
            log(f"Opening microphone at {SAMPLE_RATE} Hz (device={current_device!r})...")
            try:
                with sd.RawInputStream(
                    samplerate=SAMPLE_RATE,
                    blocksize=8000,
                    dtype="int16",
                    channels=1,
                    device=current_device,
                    callback=callback,
                ):
                    mqttc.publish(TOPIC_DEVICE_STATUS,
                                  json.dumps({"device": current_device, "status": "ok"}),
                                  retain=True)
                    log("Listening. Speak a query...")
                    while not device_change.is_set():
                        try:
                            data = audio_queue.get(timeout=0.5)
                        except queue.Empty:
                            continue

                        if reset_recognizer.is_set():
                            reset_recognizer.clear()
                            stt.recognizer = KaldiRecognizer(stt.model, SAMPLE_RATE)
                            log("Recognizer reset.")

                        if muted.is_set():
                            continue

                        result = stt.accept_audio(data)

                        if result["type"] == "final" and result["text"]:
                            text = result["text"]
                            log(f"Final: {text!r}")
                            mqttc.publish(TOPIC_TRANSCRIPT,
                                          json.dumps({"text": text, "ts": time.time()}))
                        elif result["type"] == "partial" and result["text"]:
                            print(f"\rPartial: {result['text']}", end="", flush=True)

            except Exception as e:
                log(f"Stream error: {e}")
                if not device_change.is_set():
                    time.sleep(2)   # brief pause before retry on unexpected error

            if device_change.is_set():
                current_device = next_device[0]
                log(f"Switching to device {current_device!r}")

    except KeyboardInterrupt:
        log("Interrupted.")
    finally:
        mqttc.loop_stop()
        mqttc.disconnect()
        log("Done.")


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""
WAV file → STT → MQTT publisher.
Transcribes a WAV recording with Vosk and publishes utterances to
bush/pipeline/stt/transcript, feeding the same pipeline as the live mic service.

Usage:
    python3 stt-file-service.py --file recording.wav [--model models/en-us] \
        [--delay SECONDS] [--log run.jsonl]
"""
import argparse
import json
import os
import sys
import time
import wave

import paho.mqtt.client as mqtt

from bushutil import mqtt_broker as _windows_host_ip

# ── paths ──────────────────────────────────────────────────────────────────
STT_DIR = os.environ.get("STT_DIR", "/mnt/c/Users/EB/speech-to-text")
DEFAULT_MODEL = os.environ.get("STT_MODEL", f"{STT_DIR}/models/en-us")

# ── MQTT ───────────────────────────────────────────────────────────────────
TOPIC_TRANSCRIPT = "bush/pipeline/stt/transcript"
MQTT_PORT = 1883


def log(msg: str):
    print(f"[stt-file] {msg}", flush=True)


def publish_utterance(mqttc: mqtt.Client, text: str, log_file) -> None:
    payload = {"text": text, "ts": time.time()}
    mqttc.publish(TOPIC_TRANSCRIPT, json.dumps(payload))
    log(f"Published: {text!r}")
    if log_file:
        log_file.write(json.dumps(payload) + "\n")
        log_file.flush()


def main():
    parser = argparse.ArgumentParser(description="Replay a WAV file through the Bush pipeline.")
    parser.add_argument("--file", required=True, help="Path to mono 16-bit PCM WAV file")
    parser.add_argument("--model", default=DEFAULT_MODEL, help="Vosk model path")
    parser.add_argument("--delay", type=float, default=0.0,
                        help="Pause in seconds between utterances")
    parser.add_argument("--log", help="Append utterances as JSONL to this file")
    args = parser.parse_args()

    broker = _windows_host_ip()
    log(f"MQTT broker: {broker}:{MQTT_PORT}")

    mqttc = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
    mqttc.connect(broker, MQTT_PORT, 60)
    mqttc.loop_start()
    log("MQTT connected.")

    sys.path.insert(0, STT_DIR)
    from transcriber import SpeechToText

    log_file = open(args.log, "a") if args.log else None
    utterance_count = 0
    start_time = time.time()

    try:
        with wave.open(args.file, "rb") as wf:
            if wf.getnchannels() != 1:
                raise ValueError("Audio file must be mono.")
            if wf.getsampwidth() != 2:
                raise ValueError("Audio file must be 16-bit PCM.")
            if wf.getcomptype() != "NONE":
                raise ValueError("Audio file must be uncompressed PCM WAV.")

            sample_rate = wf.getframerate()
            log(f"File: {args.file} ({sample_rate} Hz)")
            stt = SpeechToText(model_path=args.model, sample_rate=sample_rate)

            while True:
                data = wf.readframes(4000)
                if not data:
                    break

                result = stt.accept_audio(data)
                if result["type"] == "final" and result["text"]:
                    publish_utterance(mqttc, result["text"], log_file)
                    utterance_count += 1
                    if args.delay > 0:
                        time.sleep(args.delay)
                elif result["type"] == "partial" and result["text"]:
                    print(f"\rPartial: {result['text']}", end="", flush=True)

            tail = stt.final_result()
            if tail:
                publish_utterance(mqttc, tail, log_file)
                utterance_count += 1

    except KeyboardInterrupt:
        log("Interrupted.")
    finally:
        if log_file:
            log_file.close()
        mqttc.loop_stop()
        mqttc.disconnect()
        elapsed = time.time() - start_time
        log(f"Done. {utterance_count} utterance(s) in {elapsed:.1f}s.")


if __name__ == "__main__":
    main()

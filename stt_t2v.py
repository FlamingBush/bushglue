#!/usr/bin/env python3
"""
Listen via microphone, transcribe with Vosk STT, pipe each utterance into text-to-verse.
"""

import queue
import subprocess
import sys
import os

sys.path.insert(0, os.path.expanduser("~/speech-to-text"))

from transcriber import SpeechToText
import sounddevice as sd

MODEL_PATH = os.path.expanduser("~/speech-to-text/models/en-us")
T2V_BIN = os.path.expanduser("~/.cargo/bin/text-to-verse")
AFFECTS_DIR = os.path.expanduser("~/t2v/templates/affects")
SAMPLE_RATE = 16000


def run_t2v(text: str) -> str:
    cmd = [T2V_BIN, "--affects-dir", AFFECTS_DIR, "query", text]
    result = subprocess.run(cmd, capture_output=True, text=True)
    return result.stdout.strip()


def main():
    audio_queue: queue.Queue = queue.Queue()
    stt = SpeechToText(model_path=MODEL_PATH, sample_rate=SAMPLE_RATE)

    def callback(indata, frames, time, status):
        if status:
            print(status)
        audio_queue.put(bytes(indata))

    print("Listening... Speak a query. Press Ctrl+C to stop.")

    with sd.RawInputStream(
        samplerate=SAMPLE_RATE,
        blocksize=8000,
        dtype="int16",
        channels=1,
        callback=callback,
    ):
        while True:
            data = audio_queue.get()
            result = stt.accept_audio(data)

            if result["type"] == "partial" and result["text"]:
                print(f"\rHearing: {result['text']}", end="", flush=True)

            elif result["type"] == "final" and result["text"]:
                text = result["text"]
                print(f"\nYou said: {text}")
                print("Querying text-to-verse...")
                response = run_t2v(text)
                print(f"Response: {response}\n")
                print("Listening...")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nStopped.")

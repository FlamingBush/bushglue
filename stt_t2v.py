#!/usr/bin/env python3
"""
Hooks speech-to-text and text-to-verse together using subprocesses.
STT stdout is read line by line; each "Final:" utterance is piped into text-to-verse.
"""

import subprocess
import sys
import os

STT_DIR = os.path.expanduser("~/speech-to-text")
if not os.path.exists(STT_DIR):
    STT_DIR = "/mnt/c/Users/EB/speech-to-text"
STT_CMD = [sys.executable, os.path.join(STT_DIR, "main.py"), "--mic", "--model", "models/en-us"]

T2V_BIN = "/home/ubuntu/.cargo/bin/text-to-verse"
AFFECTS_DIR = "/mnt/c/Users/EB/t2v/templates/affects"


def run_t2v(text: str) -> str:
    result = subprocess.run(
        [T2V_BIN, "--affects-dir", AFFECTS_DIR, "query", text],
        capture_output=True,
        text=True,
        cwd=STT_DIR,
    )
    return result.stdout.strip()


def main():
    print("Starting speech-to-text... Speak a query. Press Ctrl+C to stop.\n")

    stt = subprocess.Popen(
        STT_CMD,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        cwd=STT_DIR,
    )

    try:
        for line in stt.stdout:
            line = line.strip()
            if line.startswith("Final: "):
                text = line[len("Final: "):]
                print(f"You said: {text}")
                print("Querying text-to-verse...")
                response = run_t2v(text)
                print(f"Response: {response}\n")
            elif line.startswith("Partial: "):
                print(f"\r{line}", end="", flush=True)
            elif line:
                print(line)
    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        stt.terminate()


if __name__ == "__main__":
    main()

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


def log(msg: str):
    print(f"[stt_t2v] {msg}", flush=True)


def run_t2v(text: str) -> str:
    log(f"Sending to text-to-verse: '{text}'")
    result = subprocess.run(
        [T2V_BIN, "--affects-dir", AFFECTS_DIR, "query", text],
        capture_output=True,
        text=True,
        cwd=STT_DIR,
    )
    if result.returncode != 0:
        log(f"text-to-verse error (exit {result.returncode}): {result.stderr.strip()}")
    return result.stdout.strip()


def main():
    log(f"STT dir: {STT_DIR}")
    log(f"STT command: {' '.join(STT_CMD)}")
    log(f"text-to-verse bin: {T2V_BIN}")
    log(f"Affects dir: {AFFECTS_DIR}")
    log("Launching STT subprocess...")

    stt = subprocess.Popen(
        STT_CMD,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        cwd=STT_DIR,
    )
    log(f"STT subprocess started (PID {stt.pid})")
    log("Waiting for STT output... Speak a query. Press Ctrl+C to stop.\n")

    try:
        for line in stt.stdout:
            line = line.strip()
            if not line:
                continue
            log(f"STT raw output: '{line}'")

            if line.startswith("Final: "):
                text = line[len("Final: "):]
                log(f"Final transcription received: '{text}'")
                log("Calling text-to-verse...")
                response = run_t2v(text)
                log("text-to-verse responded.")
                print(f"\nResponse: {response}\n")
                log("Listening for next query...")

            elif line.startswith("Partial: "):
                print(f"\r{line}", end="", flush=True)

            elif line.startswith("Listening"):
                log(f"STT ready: {line}")

            else:
                log(f"STT misc: {line}")

    except KeyboardInterrupt:
        print("\n")
        log("Interrupted by user.")
    finally:
        log("Terminating STT subprocess...")
        stt.terminate()
        log("Done.")


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""
Hooks speech-to-text and text-to-verse together using subprocesses.
text-to-verse runs in server mode so models stay loaded between queries.
STT stdout is read line by line; each "Final:" utterance hits the t2v HTTP API.
"""

import subprocess
import sys
import os
import time
import urllib.request
import urllib.error
import json

STT_DIR = os.path.expanduser("~/speech-to-text")
if not os.path.exists(STT_DIR):
    STT_DIR = "/mnt/c/Users/EB/speech-to-text"
STT_CMD = [sys.executable, os.path.join(STT_DIR, "main.py"), "--mic", "--model", "models/en-us"]

T2V_BIN = "/home/ubuntu/.cargo/bin/text-to-verse"
AFFECTS_DIR = "/mnt/c/Users/EB/t2v/templates/affects"
T2V_PORT = 8765
T2V_URL = f"http://localhost:{T2V_PORT}/query"


def log(msg: str):
    print(f"[stt_t2v] {msg}", flush=True)


def start_t2v_server() -> subprocess.Popen:
    log(f"Starting text-to-verse server on port {T2V_PORT}...")
    proc = subprocess.Popen(
        [T2V_BIN, "--affects-dir", AFFECTS_DIR, "serve", "--port", str(T2V_PORT)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    log(f"text-to-verse server PID {proc.pid} — waiting for it to be ready...")
    for i in range(60):
        try:
            urllib.request.urlopen(f"http://localhost:{T2V_PORT}/health", timeout=1)
            log(f"text-to-verse server ready (took ~{i}s)")
            return proc
        except (urllib.error.URLError, OSError):
            time.sleep(1)
    raise RuntimeError("text-to-verse server did not start within 60 seconds")


def query_t2v(text: str) -> str:
    payload = json.dumps({"question": text}).encode()
    req = urllib.request.Request(
        T2V_URL,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    start = time.time()
    with urllib.request.urlopen(req, timeout=10) as resp:
        elapsed = time.time() - start
        data = json.loads(resp.read())
        log(f"text-to-verse responded in {elapsed:.2f}s")
        return data.get("text") or data.get("affected_text") or str(data)


def main():
    log(f"STT dir:   {STT_DIR}")
    log(f"t2v bin:   {T2V_BIN}")
    log(f"t2v URL:   {T2V_URL}")

    t2v = start_t2v_server()

    log("Launching STT subprocess...")
    stt = subprocess.Popen(
        STT_CMD,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        cwd=STT_DIR,
    )
    log(f"STT subprocess started (PID {stt.pid})")
    log("Listening... Speak a query. Press Ctrl+C to stop.\n")

    try:
        for line in stt.stdout:
            line = line.strip()
            if not line:
                continue

            if line.startswith("Final: "):
                text = line[len("Final: "):]
                log(f"Transcribed: '{text}'")
                log("Querying text-to-verse...")
                try:
                    response = query_t2v(text)
                    print(f"\nResponse: {response}\n", flush=True)
                except Exception as e:
                    log(f"text-to-verse query failed: {e}")
                log("Listening...")

            elif line.startswith("Partial: "):
                print(f"\r{line}", end="", flush=True)

            elif line.startswith("Listening"):
                log(f"STT ready: {line}")

            else:
                log(f"STT: {line}")

    except KeyboardInterrupt:
        print("\n")
        log("Interrupted.")
    finally:
        log("Shutting down...")
        stt.terminate()
        t2v.terminate()
        log("Done.")


if __name__ == "__main__":
    main()

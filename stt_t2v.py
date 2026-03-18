#!/usr/bin/env python3
"""
Hooks speech-to-text, text-to-verse, and sentiment analysis together using subprocesses.
text-to-verse and bbsentimentqq run as servers so models stay loaded between queries.
STT stdout is read line by line; each "Final:" utterance flows through the full pipeline.
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

CHROMA_BIN = "/mnt/c/Users/EB/t2v-chroma/.venv/bin/chroma"
CHROMA_PATH = "/mnt/c/Users/EB/t2v-chroma/chromadb"
CHROMA_URL = "http://localhost:8000/api/v2/heartbeat"

SENTIMENT_DIR = "/mnt/c/Users/EB/bbsentimentqq"
SENTIMENT_PYTHON = "/home/ubuntu/bbsentimentqq-venv/bin/python3"
SENTIMENT_SCRIPT = os.path.join(SENTIMENT_DIR, "bbsentimentqq.py")
SENTIMENT_URL = "http://localhost:8585/"


def log(msg: str):
    print(f"[stt_t2v] {msg}", flush=True)


def wait_for_http(url: str, name: str, timeout: int = 120, proc: subprocess.Popen = None):
    log(f"Waiting for {name} to be ready at {url}...")
    for i in range(timeout):
        if proc and proc.poll() is not None:
            raise RuntimeError(f"{name} process exited early with code {proc.returncode}")
        try:
            urllib.request.urlopen(url, timeout=1)
            log(f"{name} ready (took ~{i}s)")
            return
        except (urllib.error.URLError, OSError):
            time.sleep(1)
    raise RuntimeError(f"{name} did not start within {timeout} seconds")


def start_chroma() -> subprocess.Popen:
    # If already running, don't start another
    try:
        urllib.request.urlopen(CHROMA_URL, timeout=1)
        log("ChromaDB already running.")
        return None
    except (urllib.error.URLError, OSError):
        pass
    log("Starting ChromaDB...")
    proc = subprocess.Popen(
        [CHROMA_BIN, "run", "--path", CHROMA_PATH],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    log(f"ChromaDB PID {proc.pid}")
    wait_for_http(CHROMA_URL, "ChromaDB", proc=proc)
    return proc


def start_t2v_server() -> subprocess.Popen:
    log(f"Starting text-to-verse server on port {T2V_PORT}...")
    proc = subprocess.Popen(
        [T2V_BIN, "--affects-dir", AFFECTS_DIR, "serve", "--port", str(T2V_PORT),
         "--disable-rerank", "--disable-registry", "--collections", "verse_embeddings"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    log(f"text-to-verse PID {proc.pid}")
    wait_for_http(f"http://localhost:{T2V_PORT}/health", "text-to-verse", proc=proc)
    # Pin embedding model in Ollama memory
    log("Pinning embedding model in Ollama memory...")
    urllib.request.urlopen(
        urllib.request.Request(
            "http://localhost:11434/api/embeddings",
            data=json.dumps({"model": "qwen3-embedding:0.6b", "prompt": "warmup", "keep_alive": -1}).encode(),
            headers={"Content-Type": "application/json"},
        ),
        timeout=30,
    )
    log("Embedding model pinned.")
    return proc


def start_sentiment_server() -> subprocess.Popen:
    log("Starting sentiment analysis server on port 8585...")
    proc = subprocess.Popen(
        [SENTIMENT_PYTHON, SENTIMENT_SCRIPT],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        cwd=SENTIMENT_DIR,
    )
    log(f"Sentiment server PID {proc.pid}")
    wait_for_http(SENTIMENT_URL, "sentiment server", timeout=180)
    return proc


def query_t2v(text: str) -> dict:
    payload = json.dumps({"question": text}).encode()
    req = urllib.request.Request(
        T2V_URL,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    start = time.time()
    with urllib.request.urlopen(req, timeout=15) as resp:
        elapsed = time.time() - start
        data = json.loads(resp.read())
        log(f"text-to-verse responded in {elapsed:.2f}s")
        return data


def query_sentiment(text: str) -> list:
    payload = json.dumps({"text": text}).encode()
    req = urllib.request.Request(
        SENTIMENT_URL,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        data = json.loads(resp.read())
        return data.get("classification", [])


def format_sentiment(scores: list) -> str:
    top = sorted(scores, key=lambda x: x["score"], reverse=True)
    return "  ".join(f"{e['label']}: {e['score']:.2f}" for e in top[:3])


def main():
    log(f"STT dir:        {STT_DIR}")
    log(f"t2v URL:        {T2V_URL}")
    log(f"Sentiment URL:  {SENTIMENT_URL}")

    chroma = start_chroma()
    t2v = start_t2v_server()
    sentiment = start_sentiment_server()

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
                    t2v_result = query_t2v(text)
                    verse_text = t2v_result.get("text", "")
                    print(f"\nVerse:     {verse_text}", flush=True)
                except Exception as e:
                    log(f"text-to-verse error: {e}")
                    continue

                log("Querying sentiment analysis...")
                try:
                    scores = query_sentiment(verse_text)
                    print(f"Sentiment: {format_sentiment(scores)}\n", flush=True)
                except Exception as e:
                    log(f"Sentiment error: {e}")

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
        sentiment.terminate()
        if chroma:
            chroma.terminate()
        log("Done.")


if __name__ == "__main__":
    main()

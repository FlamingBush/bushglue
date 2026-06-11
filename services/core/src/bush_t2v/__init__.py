#!/usr/bin/env python3
"""
text-to-verse MQTT bridge.
Starts the Rust text-to-verse binary as a subprocess (server mode),
waits for it to be healthy, then subscribes to bush/pipeline/stt/transcript
and publishes results to bush/pipeline/t2v/verse.
"""
import json
import os
import pathlib
import subprocess
import sys
import time
import urllib.error
import urllib.request

# ── text-to-verse subprocess config ────────────────────────────────────────
_REPO_ROOT = pathlib.Path(__file__).resolve().parents[4]  # bushglue/
T2V_BIN = os.environ.get("T2V_BIN", str(_REPO_ROOT / "t2v" / "target" / "release" / "text-to-verse"))
AFFECTS_DIR = os.environ.get("AFFECTS_DIR", str(_REPO_ROOT / "t2v" / "templates" / "affects"))
T2V_PORT = 8765
T2V_HEALTH_URL = f"http://localhost:{T2V_PORT}/health"
T2V_QUERY_URL = f"http://localhost:{T2V_PORT}/query"

# ── Ollama embedding model pinning ─────────────────────────────────────────
OLLAMA_EMBEDDINGS_URL = "http://localhost:11434/api/embeddings"
EMBED_MODEL = "qwen3-embedding:0.6b"

# ── MQTT ───────────────────────────────────────────────────────────────────
TOPIC_TRANSCRIPT  = "bush/pipeline/stt/transcript"
TOPIC_PROCESSING  = "bush/pipeline/t2v/processing"
TOPIC_VERSE       = "bush/pipeline/t2v/verse"


from bushutil import make_logger, run_mqtt_service

log = make_logger("t2v-service")


def wait_for_http(url: str, name: str, timeout: int = 120, proc: subprocess.Popen = None):
    log(f"Waiting for {name} at {url}...")
    for i in range(timeout):
        if proc and proc.poll() is not None:
            raise RuntimeError(f"{name} exited early with code {proc.returncode}")
        try:
            urllib.request.urlopen(url, timeout=1)
            log(f"{name} ready (took ~{i}s)")
            return
        except (urllib.error.URLError, OSError):
            time.sleep(1)
    raise RuntimeError(f"{name} did not start within {timeout}s")


def query_t2v(text: str) -> dict:
    payload = json.dumps({"question": text}).encode()
    req = urllib.request.Request(
        T2V_QUERY_URL,
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


def main():
    # Start t2v Rust binary
    log(f"Starting text-to-verse on port {T2V_PORT}...")
    t2v_proc = subprocess.Popen(
        [
            T2V_BIN,
            "--affects-dir", AFFECTS_DIR,
            "serve",
            "--port", str(T2V_PORT),
            "--disable-rerank",
            "--disable-registry",
            "--collections", "verse_embeddings",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    log(f"text-to-verse PID {t2v_proc.pid}")

    try:
        wait_for_http(T2V_HEALTH_URL, "text-to-verse", timeout=120, proc=t2v_proc)
    except RuntimeError as e:
        log(f"ERROR: {e}")
        sys.exit(1)

    # Pin embedding model in Ollama
    log("Pinning embedding model in Ollama...")
    try:
        urllib.request.urlopen(
            urllib.request.Request(
                OLLAMA_EMBEDDINGS_URL,
                data=json.dumps({"model": EMBED_MODEL, "prompt": "warmup", "keep_alive": -1}).encode(),
                headers={"Content-Type": "application/json"},
            ),
            timeout=30,
        )
        log("Embedding model pinned.")
    except Exception as e:
        log(f"Warning: could not pin embedding model: {e}")

    def on_message(client, userdata, msg):
        try:
            data = json.loads(msg.payload)
            text = data.get("text", "").strip()
            if not text:
                return
            log(f"Received transcript: {text!r}")
            client.publish(TOPIC_PROCESSING, json.dumps({"text": text, "ts": time.time()}))
            try:
                result = query_t2v(text)
                verse_text = result.get("text", "")
                log(f"Verse: {verse_text!r}")
                payload = json.dumps({"query": text, "text": verse_text, "ts": time.time()})
                client.publish(TOPIC_VERSE, payload)
            except Exception as e:
                log(f"text-to-verse query error: {e}")
        except Exception as e:
            log(f"Message handling error: {e}")

    try:
        run_mqtt_service("t2v-service", [TOPIC_TRANSCRIPT], on_message,
                         on_shutdown=t2v_proc.terminate)
    finally:
        # Non-signal loop exit (signal path terminates via on_shutdown)
        t2v_proc.terminate()


if __name__ == "__main__":
    main()

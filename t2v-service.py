#!/usr/bin/env python3
"""
text-to-verse MQTT bridge.
Starts the Rust text-to-verse binary as a subprocess (server mode),
waits for it to be healthy, then subscribes to bush/pipeline/stt/transcript
and publishes results to bush/pipeline/t2v/verse.

Environment variables:
  T2V_BIN      path to text-to-verse binary (default: ~/.cargo/bin/text-to-verse)
  AFFECTS_DIR  path to affect template directory (required)
"""
import json
import os
import signal
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from typing import Optional

import paho.mqtt.client as mqtt

# ── text-to-verse subprocess config ────────────────────────────────────────
T2V_BIN     = os.environ.get("T2V_BIN", os.path.expanduser("~/.cargo/bin/text-to-verse"))
AFFECTS_DIR = os.environ.get("AFFECTS_DIR")
if not AFFECTS_DIR:
    sys.exit(
        "[t2v-service] FATAL: AFFECTS_DIR env var is not set. "
        "Set it to the path of the t2v/templates/affects directory."
    )
T2V_PORT        = 8765
T2V_HEALTH_URL  = f"http://localhost:{T2V_PORT}/health"
T2V_QUERY_URL   = f"http://localhost:{T2V_PORT}/query"

# ── Ollama embedding model pinning ─────────────────────────────────────────
OLLAMA_EMBEDDINGS_URL = "http://localhost:11434/api/embeddings"
EMBED_MODEL = "qwen3-embedding:0.6b"

# ── MQTT ───────────────────────────────────────────────────────────────────
TOPIC_TRANSCRIPT  = "bush/pipeline/stt/transcript"
TOPIC_PROCESSING  = "bush/pipeline/t2v/processing"
TOPIC_VERSE       = "bush/pipeline/t2v/verse"
MQTT_PORT = 1883


from bushutil import get_mqtt_broker  # noqa: E402


def log(msg: str):
    print(f"[t2v-service] {msg}", flush=True)


def wait_for_http(url: str, name: str, timeout: int = 120, proc: Optional[subprocess.Popen] = None):
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
    broker = get_mqtt_broker()
    log(f"MQTT broker:  {broker}:{MQTT_PORT}")
    log(f"T2V_BIN:      {T2V_BIN}")
    log(f"AFFECTS_DIR:  {AFFECTS_DIR}")

    if not os.path.isfile(T2V_BIN):
        sys.exit(
            f"[t2v-service] FATAL: text-to-verse binary not found at {T2V_BIN!r}. "
            "Build it with `cargo install --path .` in the t2v repo, or set T2V_BIN."
        )

    # ── Start t2v Rust binary ──────────────────────────────────────────────
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

    # ── Shutdown handler ───────────────────────────────────────────────────
    _shutdown_called = threading.Event()

    def _shutdown(signum, frame):
        if _shutdown_called.is_set():
            return
        _shutdown_called.set()
        log("Shutting down t2v subprocess...")
        try:
            t2v_proc.terminate()
            t2v_proc.wait(timeout=5)
        except Exception:
            t2v_proc.kill()
        sys.exit(0)

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    try:
        wait_for_http(T2V_HEALTH_URL, "text-to-verse", timeout=120, proc=t2v_proc)
    except RuntimeError as e:
        log(f"ERROR: {e}")
        sys.exit(1)

    # ── Watchdog: exit if t2v process dies so systemd restarts us ─────────
    def _watchdog():
        t2v_proc.wait()
        if not _shutdown_called.is_set():
            rc = t2v_proc.returncode
            stderr = ""
            try:
                stderr = t2v_proc.stderr.read().decode(errors="replace").strip()
            except Exception:
                pass
            log(f"FATAL: text-to-verse exited unexpectedly (rc={rc}): {stderr}")
            sys.exit(1)

    threading.Thread(target=_watchdog, daemon=True).start()

    # ── Pin embedding model in Ollama ──────────────────────────────────────
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

    # ── MQTT ───────────────────────────────────────────────────────────────
    mqttc = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)

    def on_connect(client, userdata, flags, reason_code, properties):
        # Subscribe inside on_connect so subscriptions survive broker reconnects
        client.subscribe(TOPIC_TRANSCRIPT)
        log(f"MQTT connected — subscribed to {TOPIC_TRANSCRIPT}")

    def on_disconnect(client, userdata, flags, reason_code, properties):
        log(f"MQTT disconnected (rc={reason_code}) — will reconnect automatically")

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

    mqttc.on_connect = on_connect
    mqttc.on_disconnect = on_disconnect
    mqttc.on_message = on_message
    mqttc.connect(broker, MQTT_PORT, 60)

    try:
        mqttc.loop_forever()
    finally:
        log("MQTT loop stopped. Terminating t2v subprocess...")
        t2v_proc.terminate()
        mqttc.disconnect()
        log("Done.")


if __name__ == "__main__":
    main()

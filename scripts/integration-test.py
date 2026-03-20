#!/usr/bin/env python3
"""
End-to-end integration test for the Bush Glue pipeline.

Default: synthesizes a phrase to WAV via inject.py (loopback → stt-service),
testing the full audio stack. Each pipeline stage is verified in order:

  inject audio -> stt/transcript -> t2v/verse -> tts/speaking
               -> sentiment/result -> flame pulses -> tts/done

--transcript-only: skips audio injection and publishes the transcript directly
to MQTT, bypassing STT hardware. Useful when the loopback device isn't available.

Usage:
    python3 integration-test.py
    python3 integration-test.py --phrase "speak of the burning bush"
    python3 integration-test.py --transcript-only
    python3 integration-test.py --broker 192.168.1.10
    python3 integration-test.py --health-only
"""
import argparse
import json
import subprocess
import sys
import threading
import time
from pathlib import Path

import paho.mqtt.client as mqtt

# ── timeouts (seconds from the start of each phase) ─────────────────────────
T_TRANSCRIPT = 30   # audio synthesis + loopback playback + vosk processing
T_VERSE      = 45   # t2v can be slow (Ollama + Rust binary)
T_SPEAKING   =  8   # tts/speaking after verse arrives
T_SENTIMENT  = 10   # sentiment/result after verse arrives
T_PULSE      = 15   # first flame pulse after verse arrives
T_DONE       = 90   # tts/done after verse arrives (long verse + reverb tail)

# ── MQTT topics ──────────────────────────────────────────────────────────────
TOPIC_TRANSCRIPT = "bush/pipeline/stt/transcript"
TOPIC_VERSE      = "bush/pipeline/t2v/verse"
TOPIC_SPEAKING   = "bush/pipeline/tts/speaking"
TOPIC_DONE       = "bush/pipeline/tts/done"
TOPIC_SENTIMENT  = "bush/pipeline/sentiment/result"
TOPIC_FLARE      = "bush/flame/flare/pulse"

MQTT_PORT = 1883

SCRIPTS_DIR = Path(__file__).parent

# ── systemd units to check ────────────────────────────────────────────────────
HEALTH_UNITS = [
    "mosquitto",
    "chromadb",
    "bush-audio-agent",
    "bush-stt",
    "bush-t2v",
    "bush-tts",
    "bush-sentiment",
]


class Stage:
    def __init__(self, name, timeout):
        self.name    = name
        self.timeout = timeout   # seconds from phase start (inject or transcript)
        self.event   = threading.Event()
        self.payload = None
        self.elapsed = None      # seconds from phase start to receipt

    def receive(self, payload, phase_start):
        self.payload = payload
        self.elapsed = time.time() - phase_start
        self.event.set()

    def wait_until(self, deadline):
        return self.event.wait(timeout=max(0, deadline - time.time()))


def check_health() -> bool:
    """Query systemd for each service unit and print a status table."""
    print("Service health\n")
    width = max(len(u) for u in HEALTH_UNITS)
    all_active = True
    for unit in HEALTH_UNITS:
        result = subprocess.run(
            ["systemctl", "is-active", unit],
            capture_output=True, text=True,
        )
        state = result.stdout.strip()
        ok = state == "active"
        if not ok:
            all_active = False
        marker = "UP  " if ok else "DOWN"
        print(f"  {marker}  {unit:<{width}}  {state}")
    print()
    return all_active


def run_test(broker: str, phrase: str, transcript_only: bool, skip_health: bool = False) -> bool:
    # transcript stage only exists in inject (default) mode
    transcript_stage = Stage("stt/transcript", T_TRANSCRIPT)

    # these stages are keyed by topic; base time is set when transcript arrives
    downstream = {
        TOPIC_VERSE:     Stage("t2v/verse",        T_VERSE),
        TOPIC_SPEAKING:  Stage("tts/speaking",     T_SPEAKING),
        TOPIC_SENTIMENT: Stage("sentiment/result", T_SENTIMENT),
        TOPIC_FLARE:     Stage("flare pulse",      T_PULSE),
        TOPIC_DONE:      Stage("tts/done",         T_DONE),
    }

    inject_time     = [None]
    transcript_time = [None]
    connected       = threading.Event()

    subscribe_topics = list(downstream.keys())
    if not transcript_only:
        subscribe_topics.append(TOPIC_TRANSCRIPT)

    def on_connect(client, userdata, flags, rc, properties=None):
        for t in subscribe_topics:
            client.subscribe(t)
        connected.set()

    def on_message(client, userdata, msg):
        now = time.time()
        if msg.topic == TOPIC_TRANSCRIPT and not transcript_only:
            if not transcript_stage.event.is_set():
                transcript_stage.receive(msg.payload, inject_time[0])
                transcript_time[0] = now
            return
        stage = downstream.get(msg.topic)
        if stage and not stage.event.is_set() and transcript_time[0] is not None:
            stage.receive(msg.payload, transcript_time[0])

    mqttc = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
    mqttc.on_connect = on_connect
    mqttc.on_message = on_message

    if not skip_health and not check_health():
        print("Aborting: one or more services are down.")
        return False
    print(f"Connecting to {broker}:{MQTT_PORT} ...")
    try:
        mqttc.connect(broker, MQTT_PORT, 60)
    except Exception as e:
        print(f"FAIL  cannot connect to broker: {e}")
        return False

    mqttc.loop_start()
    if not connected.wait(timeout=5):
        print("FAIL  timed out waiting for MQTT subscriptions")
        mqttc.loop_stop()
        return False

    # ── inject ───────────────────────────────────────────────────────────────
    if transcript_only:
        inject_time[0] = time.time()
        transcript_time[0] = inject_time[0]
        mqttc.publish(TOPIC_TRANSCRIPT, json.dumps({"text": phrase, "ts": inject_time[0]}))
        print(f'Injected transcript: "{phrase}"\n')
    else:
        inject_time[0] = time.time()
        print(f'Injecting audio via loopback: "{phrase}"\n')
        inject_script = SCRIPTS_DIR / "inject.py"
        threading.Thread(
            target=subprocess.run,
            args=(["python3", str(inject_script), "--phrase", phrase],),
            kwargs={"stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL},
            daemon=True,
        ).start()

    # ── wait for each stage ──────────────────────────────────────────────────
    # Build the full ordered list up front so Ctrl+C can show remaining as skipped.
    pending = []
    if not transcript_only:
        pending.append((transcript_stage, inject_time))   # base time = inject
    for topic in [TOPIC_VERSE, TOPIC_SPEAKING, TOPIC_SENTIMENT,
                  TOPIC_FLARE, TOPIC_DONE]:
        pending.append((downstream[topic], transcript_time))  # base time = transcript

    results = []   # list of (stage, ok)  where ok is True/False/None (None = skipped)
    try:
        for stage, base_ref in pending:
            if base_ref[0] is None:
                # base time not yet known (transcript never arrived) — skip rest
                results.append((stage, None))
                continue
            deadline = base_ref[0] + stage.timeout
            ok = stage.wait_until(deadline)
            results.append((stage, ok))
            if not ok and stage is transcript_stage:
                # STT never fired; mark rest as skipped
                for s, _ in pending[len(results):]:
                    results.append((s, None))
                break
    except KeyboardInterrupt:
        print()
        for s, _ in pending[len(results):]:
            results.append((s, None))

    mqttc.loop_stop()
    mqttc.disconnect()

    # ── post-checks ──────────────────────────────────────────────────────────
    speaking_stage = downstream[TOPIC_SPEAKING]
    done_stage     = downstream[TOPIC_DONE]
    if speaking_stage.event.is_set() and done_stage.event.is_set():
        gap = done_stage.elapsed - speaking_stage.elapsed
        if gap < 2.0:
            # replace done's result with a failure
            results = [(s, (f"tts/done only {gap:.1f}s after speaking (want ≥2s)" if s is done_stage else ok))
                       for s, ok in results]

    return _print_results(results)


def _print_results(results):
    width = max(len(s.name) for s, _ in results)
    all_passed = True
    for stage, ok in results:
        if ok is True:
            detail = f"{stage.elapsed:.1f}s"
            if stage.payload:
                try:
                    data = json.loads(stage.payload)
                    if isinstance(data, dict) and "text" in data:
                        snippet = data["text"][:60].replace("\n", " ")
                        detail += f'  "{snippet}"'
                except Exception:
                    pass
            print(f"  PASS  {stage.name:<{width}}  {detail}")
        elif ok is False:
            print(f"  FAIL  {stage.name:<{width}}  no response within {stage.timeout}s")
            all_passed = False
        elif isinstance(ok, str):
            print(f"  FAIL  {stage.name:<{width}}  {ok}")
            all_passed = False
        else:
            print(f"  skip  {stage.name:<{width}}")
            all_passed = False

    print(f"\n{'PASSED' if all_passed else 'FAILED'}")
    return all_passed


def main():
    parser = argparse.ArgumentParser(description="Bush Glue end-to-end integration test")
    parser.add_argument("--broker", default=None,
                        help="MQTT broker host (default: auto-detect via bushutil)")
    parser.add_argument("--phrase", default="what is the meaning of fire",
                        help="Phrase to inject")
    parser.add_argument("--transcript-only", action="store_true",
                        help="Skip audio injection; publish transcript directly to MQTT")
    parser.add_argument("--mqtt-only", action="store_true",
                        help="Publish transcript via MQTT only — no health checks, no audio injection, "
                             "no local system access. Defaults broker to localhost.")
    parser.add_argument("--health-only", action="store_true",
                        help="Print service health and exit")
    args = parser.parse_args()

    if args.health_only:
        sys.exit(0 if check_health() else 1)

    if args.mqtt_only:
        broker = args.broker or "localhost"
        ok = run_test(broker, args.phrase, transcript_only=True, skip_health=True)
        sys.exit(0 if ok else 1)

    broker = args.broker
    if not broker:
        from bushutil import get_mqtt_broker
        broker = get_mqtt_broker()

    ok = run_test(broker, args.phrase, args.transcript_only)
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()

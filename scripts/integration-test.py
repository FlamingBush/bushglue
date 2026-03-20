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
    python3 integration-test.py --summarize
"""
import argparse
import json
import subprocess
import sys
import threading
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Optional

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
TOPIC_BIGJET     = "bush/flame/bigjet/pulse"

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


# ── data classes ─────────────────────────────────────────────────────────────

@dataclass
class StageResult:
    name: str
    status: str             # "pass" | "fail" | "skip"
    elapsed_s: Optional[float]
    timeout_s: int
    snippet: Optional[str]
    failure_reason: Optional[str]


@dataclass
class PipelineResult:
    stages: list            # list[StageResult]
    flare_count: int
    flare_total_ms: int
    bigjet_count: int
    bigjet_total_ms: int
    total_elapsed_s: float
    passed: bool


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


def run_pipeline(broker: str, phrase: str, transcript_only: bool) -> PipelineResult:
    """Run the pipeline and return a structured PipelineResult."""
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

    # pulse counters (window: transcript → tts/done)
    flare_count     = [0]
    flare_total_ms  = [0]
    bigjet_count    = [0]
    bigjet_total_ms = [0]

    subscribe_topics = list(downstream.keys()) + [TOPIC_BIGJET]
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

        # pulse counting (outside the Stage mechanism)
        window_open = transcript_time[0] is not None and not downstream[TOPIC_DONE].event.is_set()
        if msg.topic == TOPIC_FLARE and window_open:
            try:
                flare_count[0]    += 1
                flare_total_ms[0] += int(msg.payload)
            except (ValueError, TypeError):
                pass
        elif msg.topic == TOPIC_BIGJET and window_open:
            try:
                bigjet_count[0]    += 1
                bigjet_total_ms[0] += int(msg.payload)
            except (ValueError, TypeError):
                pass

        stage = downstream.get(msg.topic)
        if stage and not stage.event.is_set() and transcript_time[0] is not None:
            stage.receive(msg.payload, transcript_time[0])

    mqttc = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
    mqttc.on_connect = on_connect
    mqttc.on_message = on_message

    print(f"Connecting to {broker}:{MQTT_PORT} ...")
    try:
        mqttc.connect(broker, MQTT_PORT, 60)
    except Exception as e:
        print(f"FAIL  cannot connect to broker: {e}")
        # return a failed result with no stages
        return PipelineResult(
            stages=[], flare_count=0, flare_total_ms=0,
            bigjet_count=0, bigjet_total_ms=0,
            total_elapsed_s=0.0, passed=False,
        )

    mqttc.loop_start()
    if not connected.wait(timeout=5):
        print("FAIL  timed out waiting for MQTT subscriptions")
        mqttc.loop_stop()
        return PipelineResult(
            stages=[], flare_count=0, flare_total_ms=0,
            bigjet_count=0, bigjet_total_ms=0,
            total_elapsed_s=0.0, passed=False,
        )

    pipeline_start = time.time()

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

    raw_results = []   # list of (stage, ok)  where ok is True/False/None/str
    try:
        for stage, base_ref in pending:
            if base_ref[0] is None:
                # base time not yet known (transcript never arrived) — skip rest
                raw_results.append((stage, None))
                continue
            deadline = base_ref[0] + stage.timeout
            ok = stage.wait_until(deadline)
            raw_results.append((stage, ok))
            if not ok and stage is transcript_stage:
                # STT never fired; mark rest as skipped
                for s, _ in pending[len(raw_results):]:
                    raw_results.append((s, None))
                break
    except KeyboardInterrupt:
        print()
        for s, _ in pending[len(raw_results):]:
            raw_results.append((s, None))

    mqttc.loop_stop()
    mqttc.disconnect()

    total_elapsed = time.time() - pipeline_start

    # ── post-checks ──────────────────────────────────────────────────────────
    speaking_stage = downstream[TOPIC_SPEAKING]
    done_stage     = downstream[TOPIC_DONE]
    if speaking_stage.event.is_set() and done_stage.event.is_set():
        gap = done_stage.elapsed - speaking_stage.elapsed
        if gap < 2.0:
            raw_results = [
                (s, (f"tts/done only {gap:.1f}s after speaking (want ≥2s)" if s is done_stage else ok))
                for s, ok in raw_results
            ]

    # ── build structured result ───────────────────────────────────────────────
    stage_results = []
    all_passed = True
    for stage, ok in raw_results:
        snippet = None
        if ok is True and stage.payload:
            try:
                data = json.loads(stage.payload)
                if isinstance(data, dict) and "text" in data:
                    snippet = data["text"][:80].replace("\n", " ")
            except Exception:
                pass
        if ok is True:
            sr = StageResult(name=stage.name, status="pass",
                             elapsed_s=stage.elapsed, timeout_s=stage.timeout,
                             snippet=snippet, failure_reason=None)
        elif ok is False:
            sr = StageResult(name=stage.name, status="fail",
                             elapsed_s=None, timeout_s=stage.timeout,
                             snippet=None, failure_reason=f"no response within {stage.timeout}s")
            all_passed = False
        elif isinstance(ok, str):
            sr = StageResult(name=stage.name, status="fail",
                             elapsed_s=stage.elapsed, timeout_s=stage.timeout,
                             snippet=snippet, failure_reason=ok)
            all_passed = False
        else:
            sr = StageResult(name=stage.name, status="skip",
                             elapsed_s=None, timeout_s=stage.timeout,
                             snippet=None, failure_reason=None)
            all_passed = False
        stage_results.append(sr)

    return PipelineResult(
        stages=stage_results,
        flare_count=flare_count[0],
        flare_total_ms=flare_total_ms[0],
        bigjet_count=bigjet_count[0],
        bigjet_total_ms=bigjet_total_ms[0],
        total_elapsed_s=total_elapsed,
        passed=all_passed,
    )


def run_test(broker: str, phrase: str, transcript_only: bool, skip_health: bool = False) -> bool:
    if not skip_health and not check_health():
        print("Aborting: one or more services are down.")
        return False
    result = run_pipeline(broker, phrase, transcript_only)
    return _print_results(result)


def _print_results(result: PipelineResult) -> bool:
    if not result.stages:
        print("FAILED  (no stages completed)")
        return False

    width = max(len(s.name) for s in result.stages)
    for sr in result.stages:
        if sr.status == "pass":
            detail = f"{sr.elapsed_s:.1f}s"
            if sr.snippet:
                detail += f'  "{sr.snippet}"'
            print(f"  PASS  {sr.name:<{width}}  {detail}")
        elif sr.status == "fail":
            print(f"  FAIL  {sr.name:<{width}}  {sr.failure_reason}")
        else:
            print(f"  skip  {sr.name:<{width}}")

    if result.flare_count or result.bigjet_count:
        print(f"\n  flare:  {result.flare_count} pulses  {result.flare_total_ms}ms total")
        print(f"  bigjet: {result.bigjet_count} pulses  {result.bigjet_total_ms}ms total")

    print(f"\n{'PASSED' if result.passed else 'FAILED'}  ({result.total_elapsed_s:.1f}s)")
    return result.passed


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
    parser.add_argument("--summarize", action="store_true",
                        help="Output PipelineResult as JSON instead of human-readable text")
    args = parser.parse_args()

    if args.health_only:
        sys.exit(0 if check_health() else 1)

    if args.mqtt_only:
        broker = args.broker or "localhost"
        if args.summarize:
            result = run_pipeline(broker, args.phrase, transcript_only=True)
            print(json.dumps(asdict(result), indent=2))
            sys.exit(0 if result.passed else 1)
        ok = run_test(broker, args.phrase, transcript_only=True, skip_health=True)
        sys.exit(0 if ok else 1)

    broker = args.broker
    if not broker:
        from bushutil import get_mqtt_broker
        broker = get_mqtt_broker()

    if args.summarize:
        result = run_pipeline(broker, args.phrase, args.transcript_only)
        print(json.dumps(asdict(result), indent=2))
        sys.exit(0 if result.passed else 1)

    ok = run_test(broker, args.phrase, args.transcript_only)
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()

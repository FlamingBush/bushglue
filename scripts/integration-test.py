#!/usr/bin/env python3
"""
End-to-end integration test for the Bush Glue pipeline.

Injects a transcript directly into MQTT and verifies that every downstream
stage responds within its expected window:

  transcript -> t2v/verse -> tts/speaking + sentiment/result + flame pulses -> tts/done

Usage:
    python3 integration-test.py [--broker HOST] [--phrase "text to inject"]

Run on the Odroid after deploying, or pass --broker to test remotely.
"""
import argparse
import json
import sys
import threading
import time

import paho.mqtt.client as mqtt

# ── timeouts (seconds) ──────────────────────────────────────────────────────
T_VERSE     = 45   # t2v can be slow (Ollama + Rust binary)
T_SPEAKING  =  8   # tts/speaking after verse
T_SENTIMENT = 10   # sentiment/result after verse
T_PULSE     = 15   # first flame pulse after verse
T_DONE      = 90   # tts/done after verse (long verse + reverb tail)

# ── MQTT topics ─────────────────────────────────────────────────────────────
TOPIC_TRANSCRIPT = "bush/pipeline/stt/transcript"
TOPIC_VERSE      = "bush/pipeline/t2v/verse"
TOPIC_SPEAKING   = "bush/pipeline/tts/speaking"
TOPIC_DONE       = "bush/pipeline/tts/done"
TOPIC_SENTIMENT  = "bush/pipeline/sentiment/result"
TOPIC_FLARE      = "bush/flame/flare/pulse"
TOPIC_BIGJET     = "bush/flame/bigjet/pulse"

SUBSCRIBE_TOPICS = [
    TOPIC_VERSE, TOPIC_SPEAKING, TOPIC_DONE,
    TOPIC_SENTIMENT, TOPIC_FLARE, TOPIC_BIGJET,
]

MQTT_PORT = 1883

# ── result tracking ─────────────────────────────────────────────────────────
class Stage:
    def __init__(self, name, timeout):
        self.name    = name
        self.timeout = timeout
        self.event   = threading.Event()
        self.payload = None
        self.elapsed = None   # seconds from inject to receipt

    def receive(self, payload, inject_time):
        self.payload = payload
        self.elapsed = time.time() - inject_time
        self.event.set()

    def wait(self, deadline):
        remaining = deadline - time.time()
        return self.event.wait(timeout=max(0, remaining))


def run_test(broker: str, phrase: str) -> bool:
    stages = {
        TOPIC_VERSE:     Stage("t2v/verse",        T_VERSE),
        TOPIC_SPEAKING:  Stage("tts/speaking",     T_SPEAKING),
        TOPIC_SENTIMENT: Stage("sentiment/result", T_SENTIMENT),
        TOPIC_FLARE:     Stage("flare pulse",      T_PULSE),
        TOPIC_BIGJET:    Stage("bigjet pulse",      T_PULSE),
        TOPIC_DONE:      Stage("tts/done",         T_DONE),
    }

    inject_time   = [None]
    connected     = threading.Event()

    def on_connect(client, userdata, flags, rc, properties=None):
        for t in SUBSCRIBE_TOPICS:
            client.subscribe(t)
        connected.set()

    def on_message(client, userdata, msg):
        if inject_time[0] is None:
            return
        stage = stages.get(msg.topic)
        if stage and not stage.event.is_set():
            stage.receive(msg.payload, inject_time[0])

    mqttc = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
    mqttc.on_connect = on_connect
    mqttc.on_message = on_message

    print(f"Connecting to {broker}:{MQTT_PORT} ...")
    try:
        mqttc.connect(broker, MQTT_PORT, 60)
    except Exception as e:
        print(f"FAIL  cannot connect to broker: {e}")
        return False

    mqttc.loop_start()
    if not connected.wait(timeout=5):
        print("FAIL  broker connected but subscriptions timed out")
        mqttc.loop_stop()
        return False

    # ── inject ───────────────────────────────────────────────────────────────
    payload = json.dumps({"text": phrase, "ts": time.time()})
    inject_time[0] = time.time()
    mqttc.publish(TOPIC_TRANSCRIPT, payload)
    print(f'Injected: "{phrase}"\n')

    # ── wait for each stage in pipeline order ────────────────────────────────
    ordered = [
        TOPIC_VERSE,
        TOPIC_SPEAKING,
        TOPIC_SENTIMENT,
        TOPIC_FLARE,
        TOPIC_BIGJET,
        TOPIC_DONE,
    ]

    # Each stage deadline is relative to inject time, not the previous stage
    results = []
    for topic in ordered:
        stage    = stages[topic]
        deadline = inject_time[0] + stage.timeout
        ok       = stage.wait(deadline)
        results.append((stage, ok))

    mqttc.loop_stop()
    mqttc.disconnect()

    # ── report ───────────────────────────────────────────────────────────────
    width = max(len(s.name) for s, _ in results)
    all_passed = True
    for stage, ok in results:
        if ok:
            marker = "PASS"
            detail = f"{stage.elapsed:.1f}s"
            # add a snippet for key stages
            if stage.payload:
                try:
                    data = json.loads(stage.payload)
                    if "text" in data:
                        snippet = data["text"][:60].replace("\n", " ")
                        detail += f'  "{snippet}"'
                    elif isinstance(data, (int, float)):
                        detail += f"  {data}ms"
                except Exception:
                    detail += f"  {stage.payload[:40]}"
        else:
            marker   = "FAIL"
            detail   = f"no response within {stage.timeout}s"
            all_passed = False

        print(f"  {marker}  {stage.name:<{width}}  {detail}")

    total = time.time() - inject_time[0]
    print(f"\n{'PASSED' if all_passed else 'FAILED'}  ({total:.1f}s total)")
    return all_passed


def main():
    parser = argparse.ArgumentParser(description="Bush Glue end-to-end integration test")
    parser.add_argument("--broker", default=None,
                        help="MQTT broker host (default: auto-detect via bushutil)")
    parser.add_argument("--phrase", default="what is the meaning of fire",
                        help="Phrase to inject as test transcript")
    args = parser.parse_args()

    if args.broker:
        broker = args.broker
    else:
        from bushutil import get_mqtt_broker
        broker = get_mqtt_broker()

    ok = run_test(broker, args.phrase)
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()

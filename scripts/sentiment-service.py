#!/usr/bin/env python3

# For text-classificaiton
# Requires transformers and torch
from transformers import pipeline

# For serving the http interface


# Text classifier
# see https://huggingface.co/bhadresh-savani/distilbert-base-uncased-emotion?text=I+feel+a+bit+let+down
# Create the classifier
classifier = pipeline("text-classification",model='bhadresh-savani/distilbert-base-uncased-emotion', return_all_scores=True)
# Warm it up with a throw-away execution which gets it to download the model, and then load that model
classifier("Weeeeee!", )


# For the HTTP server
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import random
import threading
import time

import paho.mqtt.client as mqtt

from bushutil import mqtt_broker

# ── MQTT topics ────────────────────────────────────────────────────────────
TOPIC_VERSE    = "bush/pipeline/t2v/verse"
TOPIC_SENTIMENT = "bush/pipeline/sentiment/result"
TOPIC_FLARE    = "bush/flame/flare/pulse"
TOPIC_BIGJET   = "bush/flame/bigjet/pulse"
TOPIC_TTS_DONE = "bush/pipeline/tts/done"
MQTT_PORT = 1883

# ── emotion fire patterns ──────────────────────────────────────────────────
# Each emotion drives a different pulse rhythm for the duration of TTS speech.
#
#   flare_ms      — valve-open time per flare pulse
#   flare_period  — time between pulse starts (ms); must be > flare_ms for gaps
#   bigjet_ms     — valve-open time per bigjet pulse (0 = no bigjet)
#   bigjet_period — time between bigjet pulses (ms)
#   jitter        — random ±fraction applied to timings each cycle
#
EMOTION_PATTERNS = {
    #                flare_ms  period   bigjet_ms  bj_period  jitter
    "anger":   dict(flare_ms=220, flare_period=260,  bigjet_ms=700, bigjet_period=3500, jitter=0.15),
    "joy":     dict(flare_ms=450, flare_period=650,  bigjet_ms=0,   bigjet_period=0,    jitter=0.20),
    "love":    dict(flare_ms=700, flare_period=1100, bigjet_ms=0,   bigjet_period=0,    jitter=0.25),
    "surprise":dict(flare_ms=280, flare_period=340,  bigjet_ms=450, bigjet_period=4500, jitter=0.30),
    "fear":    dict(flare_ms=90,  flare_period=380,  bigjet_ms=0,   bigjet_period=0,    jitter=0.50),
    "sadness": dict(flare_ms=250, flare_period=2400, bigjet_ms=0,   bigjet_period=0,    jitter=0.20),
}


# ── fire pattern runner ────────────────────────────────────────────────────

def _fire_loop(pattern: dict, score: float, mqttc: mqtt.Client, stop: threading.Event):
    """Publish pulsed fire commands until stop is set (i.e. TTS finishes)."""
    flare_ms    = int(pattern["flare_ms"]  * score)
    bigjet_ms   = int(pattern["bigjet_ms"] * score)
    flare_period = pattern["flare_period"]
    bigjet_period = pattern["bigjet_period"]
    jitter       = pattern["jitter"]
    last_bigjet  = 0.0

    while not stop.is_set():
        # flare pulse
        if flare_ms > 0:
            v = flare_ms * (1 + jitter * (random.random() * 2 - 1))
            mqttc.publish(TOPIC_FLARE, max(50, int(v)))

        # bigjet pulse on its own slower clock
        if bigjet_ms > 0 and bigjet_period > 0:
            now = time.monotonic()
            if (now - last_bigjet) * 1000 >= bigjet_period:
                v = bigjet_ms * (1 + jitter * (random.random() * 2 - 1))
                mqttc.publish(TOPIC_BIGJET, max(100, int(v)))
                last_bigjet = now

        # wait for next flare period (with jitter), or until stopped
        p = flare_period * (1 + jitter * (random.random() * 2 - 1))
        stop.wait(p / 1000)


_fire_stop: threading.Event | None = None
_fire_lock = threading.Lock()


def _stop_fire():
    global _fire_stop
    with _fire_lock:
        if _fire_stop:
            _fire_stop.set()
            _fire_stop = None


def _start_fire(pattern: dict, score: float, mqttc: mqtt.Client):
    global _fire_stop
    _stop_fire()
    stop = threading.Event()
    with _fire_lock:
        _fire_stop = stop
    threading.Thread(target=_fire_loop, args=(pattern, score, mqttc, stop), daemon=True).start()


def _classify_and_fire(verse_text: str, mqttc: mqtt.Client):
    """Classify verse_text, start sustained fire pattern, return (scores, label, score)."""
    scores = classifier(verse_text)  # list of {label, score} dicts
    top = sorted(scores, key=lambda x: x["score"], reverse=True)[0]
    label = top["label"]
    score = top["score"]

    pattern = EMOTION_PATTERNS.get(label)
    if pattern:
        print(f"[sentiment] MQTT fire: emotion={label} score={score:.2f} (sustained pattern)", flush=True)
        _start_fire(pattern, score, mqttc)
    else:
        print(f"[sentiment] No pattern for emotion '{label}'", flush=True)

    # for backwards-compat with result payload, report first-pulse values
    flare  = int(pattern["flare_ms"]  * score) if pattern else 0
    bigjet = int(pattern["bigjet_ms"] * score) if pattern else 0
    return scores, flare, bigjet


def _start_mqtt_thread():
    """Start MQTT subscriber in a background thread."""
    broker = mqtt_broker()
    print(f"[sentiment] Connecting to MQTT broker {broker}:{MQTT_PORT}...", flush=True)

    mqttc = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)

    def on_connect(client, userdata, flags, reason_code, properties):
        print(f"[sentiment] MQTT connected (rc={reason_code})", flush=True)
        client.subscribe(TOPIC_VERSE)
        client.subscribe(TOPIC_TTS_DONE)
        print(f"[sentiment] Subscribed to {TOPIC_VERSE}, {TOPIC_TTS_DONE}", flush=True)

    def on_message(client, userdata, msg):
        try:
            if msg.topic == TOPIC_TTS_DONE:
                print("[sentiment] TTS done — stopping fire pattern", flush=True)
                _stop_fire()
                return

            data = json.loads(msg.payload)
            verse_text = data.get("text", "").strip()
            if not verse_text:
                return
            print(f"[sentiment] Classifying verse: {verse_text!r}", flush=True)
            scores, flare, bigjet = _classify_and_fire(verse_text, client)
            result_payload = json.dumps({
                "verse": verse_text,
                "classification": scores,
                "flare": flare,
                "bigjet": bigjet,
                "ts": time.time(),
            })
            client.publish(TOPIC_SENTIMENT, result_payload)
        except Exception as e:
            print(f"[sentiment] MQTT message error: {e}", flush=True)

    mqttc.on_connect = on_connect
    mqttc.on_message = on_message

    def _loop():
        try:
            mqttc.connect(broker, MQTT_PORT, 60)
            mqttc.loop_forever()
        except Exception as e:
            print(f"[sentiment] MQTT loop error: {e}", flush=True)

    t = threading.Thread(target=_loop, daemon=True)
    t.start()
    return t


class Server(BaseHTTPRequestHandler):
    # Our headers are always the same unless something goes wrong
    def resp(self, code, body):
        bbytes = json.dumps(body).encode()
        self.send_response(code)
        self.send_header('Content-type', 'application/json')
        self.send_header('content-length', len(bbytes))
        self.end_headers()
        self.wfile.write(json.dumps(body).encode())

    # We only really respond to POST messages
    def do_GET(self):
        self.resp(200, {})
    def do_HEAD(self):
        self.resp(200, {})

    def do_POST(self):
        length = int(self.headers.get('content-length'))
        input = json.loads(self.rfile.read(length))
        message = ""
        if 'text' in input:
            message = input['text']
        if 'affected_text' in input:
            message = input['affected_text']
        if message == "":
            self.resp(400, {'error': "Post must contain json object with affected_text or text key"})
            return

        self.resp(200, {'message': message, 'classification': classifier(message)})

if __name__ == "__main__":
    _start_mqtt_thread()
    address = ("0.0.0.0", 8585)
    httpd = HTTPServer(address, Server)
    print("Starting server ...")
    httpd.serve_forever()

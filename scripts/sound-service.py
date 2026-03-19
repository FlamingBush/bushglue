#!/usr/bin/env python3
"""
Sound effects service for Bush Glue.

Subscribes to bush/flame/flare/pulse and bush/flame/bigjet/pulse.
Each message carries valve on-time in milliseconds; the corresponding
sound plays for exactly that duration then stops.

  Flare  — warm low hum: 80 Hz fundamental + harmonics, soft-clip warmth,
            flame-flicker tremolo at ~6 Hz.

  BigJet — harsh broadband whoosh: high-pass weighted noise mixed with a
            fast-rising frequency sweep, hard-clipped for aggression.

Both effects run in separate threads so they can overlap cleanly.
"""
import json
import queue
import signal
import subprocess
import sys
import threading

import numpy as np
import paho.mqtt.client as mqtt
import sounddevice as sd

MQTT_PORT  = 1883
TOPIC_FLARE  = "bush/flame/flare/pulse"
TOPIC_BIGJET = "bush/flame/bigjet/pulse"
SR = 44100  # sample rate


def _windows_host_ip() -> str:
    try:
        with open("/proc/version") as f:
            if "microsoft" not in f.read().lower():
                return "localhost"
    except OSError:
        return "localhost"
    result = subprocess.run(["ip", "route", "show"], capture_output=True, text=True)
    for line in result.stdout.splitlines():
        if line.startswith("default"):
            return line.split()[2]
    return "localhost"


def log(msg: str):
    print(f"[sound-service] {msg}", flush=True)


# ── synthesis ───────────────────────────────────────────────────────────────

def _make_flare(duration_ms: int) -> np.ndarray:
    """Warm low roar — harmonic stack audible on small speakers + combustion noise."""
    n   = int(SR * duration_ms / 1000)
    t   = np.linspace(0, duration_ms / 1000, n, endpoint=False)
    rng = np.random.default_rng()

    # Harmonic stack rooted at 110 Hz (first harmonic small speakers can reproduce)
    # Weight mid harmonics (220–440 Hz) more heavily so the sound cuts through
    sig  = 0.30 * np.sin(2 * np.pi * 110.0 * t)
    sig += 0.40 * np.sin(2 * np.pi * 220.4 * t)   # 2nd — boosted
    sig += 0.25 * np.sin(2 * np.pi * 331.0 * t)   # 3rd
    sig += 0.18 * np.sin(2 * np.pi * 441.7 * t)   # 4th
    sig += 0.10 * np.sin(2 * np.pi * 553.0 * t)   # 5th
    sig += 0.05 * np.sin(2 * np.pi * 663.3 * t)   # 6th

    # Soft clip → warm, slightly overdriven character
    sig = np.tanh(sig * 2.2)

    # Combustion texture: bandpass noise around 250–700 Hz
    # Achieved by mixing white noise with its double-differentiated version
    # (rough bandpass without scipy)
    noise   = rng.standard_normal(n) * 0.18
    d1      = np.diff(noise, prepend=noise[0])
    d2      = np.diff(d1,    prepend=d1[0])
    texture = noise - d2 * 0.04   # emphasises mid, softens extremes
    sig    += texture

    # Flame flicker: slow tremolo ~5 Hz with randomised phase
    phase   = rng.uniform(0, 2 * np.pi)
    flicker = 0.78 + 0.22 * np.sin(2 * np.pi * 5 * t + phase)
    sig    *= flicker

    # Short fade in/out (30 ms) to avoid clicks
    fade = min(int(0.03 * SR), n // 4)
    sig[:fade]  *= np.linspace(0, 1, fade)
    sig[-fade:] *= np.linspace(1, 0, fade)

    # Normalise to peak 0.80 — loud but no clipping
    peak = np.abs(sig).max()
    if peak > 0:
        sig *= 0.80 / peak

    return sig.astype(np.float32)


def _make_bigjet(duration_ms: int) -> np.ndarray:
    """Harsh broadband whoosh — high-pass noise + rising sweep, hard-clipped."""
    n   = int(SR * duration_ms / 1000)
    t   = np.linspace(0, duration_ms / 1000, n, endpoint=False)
    rng = np.random.default_rng()

    # Broadband white noise
    noise = rng.standard_normal(n)

    # First-difference high-pass (boosts high frequencies for harshness)
    hp = np.diff(noise, prepend=noise[0])

    # Rising frequency sweep: 150 Hz → 3 kHz over the burst duration
    # gives the "whoosh" character (pressure building then cutting off)
    sweep_hz  = 150 + 2850 * (t / (duration_ms / 1000)) ** 0.6
    phase     = 2 * np.pi * np.cumsum(sweep_hz) / SR
    sweep     = np.sin(phase)

    # Mix noise body with tonal sweep
    sig = 0.65 * hp + 0.35 * sweep

    # Hard clip → aggressive, metallic edge
    sig = np.clip(sig * 1.8, -1, 1)

    # Amplitude envelope: 8 ms attack, flat body, 40 ms decay
    attack = min(int(0.008 * SR), n)
    decay  = min(int(0.040 * SR), n // 4)
    env    = np.ones(n)
    env[:attack]  *= np.linspace(0, 1, attack)
    env[-decay:]  *= np.linspace(1, 0, decay)
    sig *= env

    return (sig * 0.55).astype(np.float32)


# ── audio engine ────────────────────────────────────────────────────────────
# One persistent OutputStream keeps PulseAudio alive at all times.
# Silence fills the stream between effects; flare and bigjet mix additively.
# Synthesis happens off the MQTT thread via a request queue.

_CHUNK = 512

class AudioEngine:
    """Single persistent OutputStream; silence-fills gaps; effects mix."""

    def __init__(self):
        self._req: queue.Queue = queue.Queue()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=False)
        self._thread.start()

    def play(self, name: str, duration_ms: int, make_fn):
        """Queue a sound request; synthesis happens inside the audio thread."""
        self._req.put((name, duration_ms, make_fn))

    def stop(self):
        """Signal the audio thread to exit cleanly and wait for it."""
        self._stop.set()
        self._thread.join(timeout=2)

    def _run(self):
        try:
            with sd.OutputStream(samplerate=SR, channels=1, dtype="float32",
                                  blocksize=_CHUNK) as stream:
                log("Audio stream open — PulseAudio connected")
                buffers: dict[str, tuple[np.ndarray, int]] = {}
                silence = np.zeros(_CHUNK, dtype=np.float32)
                while not self._stop.is_set():
                    # drain all pending requests (synthesise here, not in on_message)
                    while True:
                        try:
                            name, ms, fn = self._req.get_nowait()
                            buffers[name] = (fn(ms), 0)
                        except queue.Empty:
                            break
                    # mix active buffers
                    chunk = silence.copy()
                    done = []
                    for name, (audio, pos) in buffers.items():
                        end = min(pos + _CHUNK, len(audio))
                        n = end - pos
                        chunk[:n] += audio[pos:end]
                        if end >= len(audio):
                            done.append(name)
                        else:
                            buffers[name] = (audio, end)
                    for name in done:
                        del buffers[name]
                    stream.write(chunk.reshape(-1, 1))
                log("Audio stream closed cleanly")
        except Exception as e:
            log(f"Audio engine error: {e}")


_engine = AudioEngine()


# ── MQTT ────────────────────────────────────────────────────────────────────

def on_connect(client, userdata, flags, reason_code, properties):
    log(f"MQTT connected (rc={reason_code})")
    client.subscribe(TOPIC_FLARE)
    client.subscribe(TOPIC_BIGJET)
    log(f"Subscribed to {TOPIC_FLARE}, {TOPIC_BIGJET}")


def on_message(client, userdata, msg):
    try:
        ms = int(msg.payload.decode())
        if ms <= 0:
            return
        if msg.topic == TOPIC_FLARE:
            log(f"Flare {ms} ms")
            _engine.play("flare", ms, _make_flare)
        elif msg.topic == TOPIC_BIGJET:
            log(f"BigJet {ms} ms")
            _engine.play("bigjet", ms, _make_bigjet)
    except Exception as e:
        log(f"Message error: {e}")


def main():
    broker = _windows_host_ip()
    log(f"MQTT broker: {broker}:{MQTT_PORT}")

    mqttc = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
    mqttc.on_connect = on_connect
    mqttc.on_message = on_message

    def _shutdown(signum, frame):
        log("Shutting down...")
        mqttc.loop_stop()
        mqttc.disconnect()
        _engine.stop()   # let OutputStream exit its context manager cleanly
        sys.exit(0)

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    try:
        mqttc.connect(broker, MQTT_PORT, 60)
    except Exception as e:
        log(f"Cannot connect to broker: {e}")
        sys.exit(1)

    mqttc.loop_forever()


if __name__ == "__main__":
    main()

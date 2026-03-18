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


# ── player ──────────────────────────────────────────────────────────────────

class SoundPlayer:
    """Plays one audio buffer at a time in a background thread.
    Calling play() while audio is already running interrupts and restarts."""

    def __init__(self, name: str):
        self.name  = name
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._t: threading.Thread | None = None

    def play(self, audio: np.ndarray):
        with self._lock:
            self._stop.set()
            if self._t and self._t.is_alive():
                self._t.join(timeout=0.05)
            self._stop = threading.Event()
            stop = self._stop
            self._t = threading.Thread(
                target=self._run, args=(audio, stop), daemon=True
            )
            self._t.start()

    def _run(self, audio: np.ndarray, stop: threading.Event):
        try:
            with sd.OutputStream(samplerate=SR, channels=1, dtype="float32",
                                  blocksize=512) as stream:
                pos        = 0
                chunk_size = 512
                while pos < len(audio) and not stop.is_set():
                    end = min(pos + chunk_size, len(audio))
                    stream.write(audio[pos:end].reshape(-1, 1))
                    pos = end
        except Exception as e:
            log(f"[{self.name}] playback error: {e}")


flare_player  = SoundPlayer("flare")
bigjet_player = SoundPlayer("bigjet")


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
            flare_player.play(_make_flare(ms))
        elif msg.topic == TOPIC_BIGJET:
            log(f"BigJet {ms} ms")
            bigjet_player.play(_make_bigjet(ms))
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

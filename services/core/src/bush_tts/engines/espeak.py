"""espeak-ng TTS engine adapter.

Wraps the existing espeak-ng invocation in the new engine contract. Output
is 22050 Hz mono int16 LE PCM (espeak's default --stdout WAV at the rate
we ask for, decoded back to raw via subprocess).

This is the legacy/fallback engine. The current bush-tts main loop pipes
espeak's WAV output directly into sox; the adapter version converts to raw
PCM bytes so the caller can pipe into sox identically.

Config:
  ESPEAK_VOICE   — voice flag (default: "en-gb")
  ESPEAK_SPEED   — words per minute (default: 95)
  ESPEAK_PITCH   — pitch [0-99] (default: 1)
  ESPEAK_AMP     — amplitude [0-200] (default: 200)
"""
from __future__ import annotations

import os
import subprocess
import time
import wave
from io import BytesIO
from typing import cast

from .base import SynthesisResult


def log(msg: str) -> None:
    print(f"[espeak-engine] {msg}", flush=True)


class EspeakEngine:
    name = "espeak"

    def __init__(self, voice: str = None, speed: int = None, pitch: int = None, amplitude: int = None):
        self._voice = voice or os.environ.get("ESPEAK_VOICE", "en-gb")
        self._speed = speed if speed is not None else int(os.environ.get("ESPEAK_SPEED", "95"))
        self._pitch = pitch if pitch is not None else int(os.environ.get("ESPEAK_PITCH", "1"))
        self._amplitude = amplitude if amplitude is not None else int(os.environ.get("ESPEAK_AMP", "200"))
        self._closed = False

    def synthesize(self, text: str) -> SynthesisResult:
        if self._closed:
            raise RuntimeError("EspeakEngine is closed")
        text = (text or "").strip()
        if not text:
            return cast(SynthesisResult, {"audio_pcm": b"", "sample_rate": 22050, "ts": time.time()})

        cmd = [
            "espeak-ng",
            "-v", self._voice,
            "-s", str(self._speed),
            "-p", str(self._pitch),
            "-a", str(self._amplitude),
            "--stdout",
            text,
        ]
        proc = subprocess.run(
            cmd,
            capture_output=True,
            timeout=30,
        )
        if proc.returncode != 0:
            stderr = (proc.stderr or b"")[-200:].decode(errors="replace")
            raise RuntimeError(f"espeak-ng failed (rc={proc.returncode}): {stderr}")

        # espeak --stdout produces a WAV stream; parse out the raw PCM and sample rate
        bio = BytesIO(proc.stdout)
        with wave.open(bio, "rb") as wf:
            sample_rate = wf.getframerate()
            n_channels = wf.getnchannels()
            sampwidth = wf.getsampwidth()
            if n_channels != 1 or sampwidth != 2:
                raise RuntimeError(f"unexpected espeak WAV format: {n_channels}ch / {sampwidth*8}bit")
            audio_pcm = wf.readframes(wf.getnframes())

        return cast(SynthesisResult, {
            "audio_pcm": audio_pcm,
            "sample_rate": sample_rate,
            "ts": time.time(),
        })

    def close(self) -> None:
        self._closed = True

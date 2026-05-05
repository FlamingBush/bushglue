"""Piper TTS engine adapter.

Subprocess driver to the Piper binary. Reads voice metadata from the
`<voice>.onnx.json` config that ships with each voice to determine the
real sample rate (16 kHz, 22.05 kHz, 24 kHz depending on voice).

Per D8: this is the production TTS for both bush-tts speaker output and
Discord voice channels.

Config:
  PIPER_BIN           — piper binary path (default: "piper" on PATH)
  PIPER_VOICE         — path to voice .onnx file (no default; must be set)
  PIPER_SPEAKER_ID    — for multi-speaker voices (default: 0)
  PIPER_LENGTH_SCALE  — speed multiplier (1.0 default; >1 slower, <1 faster)
"""
from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path
from typing import cast

from .base import SynthesisResult


def log(msg: str) -> None:
    print(f"[piper-engine] {msg}", flush=True)


class PiperEngine:
    name = "piper"

    def __init__(
        self,
        voice_path: str = None,
        binary_path: str = None,
        *,
        speaker_id: int = None,
        length_scale: float = None,
    ):
        self._binary = binary_path or os.environ.get("PIPER_BIN", "piper")
        self._voice_path = voice_path or os.environ.get("PIPER_VOICE")
        if not self._voice_path:
            raise RuntimeError("PIPER_VOICE must be set or voice_path passed to PiperEngine()")

        # Read voice metadata to get real sample rate
        config_path = Path(self._voice_path + ".json")
        if not config_path.exists():
            raise RuntimeError(f"Piper voice config not found: {config_path}")
        with open(config_path) as f:
            self._voice_config = json.load(f)
        self._sample_rate = int(self._voice_config.get("audio", {}).get("sample_rate", 22050))
        log(f"voice={self._voice_path}, sample_rate={self._sample_rate}")

        self._speaker_id = speaker_id if speaker_id is not None else int(os.environ.get("PIPER_SPEAKER_ID", "0"))
        self._length_scale = length_scale if length_scale is not None else float(os.environ.get("PIPER_LENGTH_SCALE", "1.0"))
        self._closed = False

    def synthesize(self, text: str) -> SynthesisResult:
        if self._closed:
            raise RuntimeError("PiperEngine is closed")
        text = (text or "").strip()
        if not text:
            return cast(SynthesisResult, {"audio_pcm": b"", "sample_rate": self._sample_rate, "ts": time.time()})

        cmd = [
            self._binary,
            "--model", self._voice_path,
            "--output_raw",
            "--length_scale", str(self._length_scale),
            "--speaker", str(self._speaker_id),
        ]
        proc = subprocess.run(
            cmd,
            input=text.encode("utf-8"),
            capture_output=True,
            timeout=60,
        )
        if proc.returncode != 0:
            stderr = (proc.stderr or b"")[-200:].decode(errors="replace")
            raise RuntimeError(f"piper failed (rc={proc.returncode}): {stderr}")

        return cast(SynthesisResult, {
            "audio_pcm": proc.stdout,
            "sample_rate": self._sample_rate,
            "ts": time.time(),
        })

    def close(self) -> None:
        self._closed = True

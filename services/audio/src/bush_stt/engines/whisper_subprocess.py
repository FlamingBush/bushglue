"""Whisper.cpp engine adapter via compiled binary subprocess.

Per D6/D7: one of two whisper integrations under bench in week 1. Calls
the whisper.cpp binary per utterance. Reproducible (no Python wheel
bet), matches the repo's existing subprocess style (parec, arecord, sox).

Config:
  WHISPER_BIN — path to whisper.cpp `whisper-cli` binary (default: "whisper-cli" on PATH)
  WHISPER_MODEL — path to GGUF model file (default: "data/whisper-models/ggml-base.en-q8_0.bin")
  WHISPER_THREADS — number of threads (default: 4)
  WHISPER_LANGUAGE — language (default: "en")
  WHISPER_TIMEOUT_S — per-utterance timeout (default: 30)

Audio format: int16 LE mono PCM at 16 kHz, written to a temporary WAV
file before invoking whisper-cli. Temp WAV is deleted after, even on error.
"""
from __future__ import annotations

import os
import struct
import subprocess
import tempfile
import time
from pathlib import Path
from typing import cast

from .base import TranscribeResult


def log(msg: str) -> None:
    print(f"[whisper-subprocess] {msg}", flush=True)


def _write_wav(path: Path, pcm: bytes, sample_rate: int = 16000) -> None:
    """Write a minimal RIFF WAV file from int16 LE mono PCM."""
    n_samples = len(pcm) // 2
    n_channels = 1
    bits = 16
    byte_rate = sample_rate * n_channels * bits // 8
    block_align = n_channels * bits // 8
    data_size = len(pcm)
    riff_size = 36 + data_size

    with open(path, "wb") as f:
        f.write(b"RIFF")
        f.write(struct.pack("<I", riff_size))
        f.write(b"WAVE")
        # fmt chunk
        f.write(b"fmt ")
        f.write(struct.pack("<I", 16))           # PCM fmt chunk size
        f.write(struct.pack("<H", 1))            # PCM format
        f.write(struct.pack("<H", n_channels))
        f.write(struct.pack("<I", sample_rate))
        f.write(struct.pack("<I", byte_rate))
        f.write(struct.pack("<H", block_align))
        f.write(struct.pack("<H", bits))
        # data chunk
        f.write(b"data")
        f.write(struct.pack("<I", data_size))
        f.write(pcm)


class WhisperSubprocessEngine:
    name = "whisper-subprocess"
    sample_rate = 16000

    def __init__(
        self,
        binary_path: str = None,
        model_path: str = None,
        *,
        n_threads: int = None,
        language: str = None,
        timeout_s: float = None,
    ) -> None:
        self._binary = binary_path or os.environ.get("WHISPER_BIN", "whisper-cli")
        self._model = model_path or os.environ.get(
            "WHISPER_MODEL", "data/whisper-models/ggml-base.en-q8_0.bin"
        )
        self._threads = n_threads if n_threads is not None else int(os.environ.get("WHISPER_THREADS", "4"))
        self._language = language or os.environ.get("WHISPER_LANGUAGE", "en")
        self._timeout_s = timeout_s if timeout_s is not None else float(os.environ.get("WHISPER_TIMEOUT_S", "30"))
        self._closed = False

        log(f"binary={self._binary}, model={self._model}, threads={self._threads}, lang={self._language}")

        # Sanity check (don't fail if missing on dev machines — we test it on the M2)
        if not Path(self._model).exists():
            log(f"WARN: model file not found at {self._model} — first transcribe will fail clearly")

    def transcribe(self, audio_pcm: bytes) -> TranscribeResult:
        if self._closed:
            raise RuntimeError("WhisperSubprocessEngine is closed")
        if not audio_pcm:
            return cast(TranscribeResult, {"text": "", "confidence": 0.0, "ts": time.time()})

        # Write WAV to a temp file
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tf:
            wav_path = Path(tf.name)
        try:
            _write_wav(wav_path, audio_pcm, self.sample_rate)
            output_base = wav_path.with_suffix("")  # whisper-cli writes <base>.txt

            cmd = [
                self._binary,
                "-m", self._model,
                "-f", str(wav_path),
                "-otxt",                # write .txt output
                "-nt",                  # no timestamps in output
                "-np",                  # no progress prints
                "-l", self._language,
                "-t", str(self._threads),
                "-of", str(output_base),
            ]
            try:
                proc = subprocess.run(
                    cmd,
                    capture_output=True,
                    timeout=self._timeout_s,
                    text=True,
                )
            except subprocess.TimeoutExpired:
                log(f"timeout after {self._timeout_s}s")
                return cast(TranscribeResult, {"text": "", "confidence": 0.0, "ts": time.time()})

            if proc.returncode != 0:
                stderr = proc.stderr[-200:] if proc.stderr else "(no stderr)"
                log(f"non-zero exit {proc.returncode}: {stderr}")
                return cast(TranscribeResult, {"text": "", "confidence": 0.0, "ts": time.time()})

            txt_path = Path(str(output_base) + ".txt")
            if not txt_path.exists():
                log(f"output file not created at {txt_path}")
                return cast(TranscribeResult, {"text": "", "confidence": 0.0, "ts": time.time()})

            text = txt_path.read_text().strip()
            confidence = 1.0 if text else 0.0
            return cast(TranscribeResult, {
                "text": text,
                "confidence": confidence,
                "ts": time.time(),
            })
        finally:
            wav_path.unlink(missing_ok=True)
            txt_artifact = Path(str(wav_path.with_suffix("")) + ".txt")
            txt_artifact.unlink(missing_ok=True)

    def close(self) -> None:
        self._closed = True

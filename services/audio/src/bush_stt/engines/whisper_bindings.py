"""Whisper.cpp engine adapter via pywhispercpp Python bindings.

Per D6/D7 (eng review): one of two whisper integrations under bench
in week 1. Loads the model once at __init__, calls model.transcribe()
per utterance.

Config:
  WHISPER_MODEL — model file path or whisper.cpp model name (default: "base.en")
  WHISPER_THREADS — number of threads for whisper.cpp (default: 4 on M2 A76 cores)
  WHISPER_LANGUAGE — language code (default: "en")
"""
from __future__ import annotations

import os
import time
from typing import cast

import numpy as np

from .base import TranscribeResult


def log(msg: str) -> None:
    print(f"[whisper-bindings] {msg}", flush=True)


class WhisperBindingsEngine:
    name = "whisper-bindings"
    sample_rate = 16000

    def __init__(
        self,
        model_path_or_name: str = None,
        *,
        n_threads: int = None,
        language: str = None,
    ) -> None:
        # Lazy import so tests can stub via sys.modules
        from pywhispercpp.model import Model

        model = model_path_or_name or os.environ.get("WHISPER_MODEL", "base.en")
        threads = n_threads if n_threads is not None else int(os.environ.get("WHISPER_THREADS", "4"))
        lang = language or os.environ.get("WHISPER_LANGUAGE", "en")

        log(f"loading model: {model}, threads={threads}, lang={lang}")
        load_start = time.time()
        self._model = Model(model, n_threads=threads, language=lang, print_realtime=False, print_progress=False)
        log(f"loaded in {(time.time() - load_start)*1000:.0f} ms")

        self._closed = False
        self._n_threads = threads
        self._language = lang

        # Warm up: synth ~250ms of silence, run one transcribe to JIT the compute graph
        warmup_samples = np.zeros(int(self.sample_rate * 0.25), dtype=np.float32)
        warmup_start = time.time()
        try:
            self._model.transcribe(warmup_samples)
            log(f"warmup complete in {(time.time() - warmup_start)*1000:.0f} ms")
        except Exception as e:
            log(f"warmup failed (non-fatal): {e}")

    def transcribe(self, audio_pcm: bytes) -> TranscribeResult:
        if self._closed:
            raise RuntimeError("WhisperBindingsEngine is closed")
        if not audio_pcm:
            return cast(TranscribeResult, {"text": "", "confidence": 0.0, "ts": time.time()})

        # int16 LE bytes -> float32 [-1, 1]
        arr = np.frombuffer(audio_pcm, dtype=np.int16).astype(np.float32) / 32768.0

        segments = self._model.transcribe(arr)
        # pywhispercpp returns either an iterator of Segment objects or a list
        text_parts: list[str] = []
        seg_count = 0
        for seg in segments:
            seg_count += 1
            t = getattr(seg, "text", None) or (seg["text"] if isinstance(seg, dict) else "")
            if t:
                text_parts.append(t)
        text = " ".join(p.strip() for p in text_parts).strip()

        # No direct confidence; use simple proxy (1.0 if any segments, else 0.0)
        confidence = 1.0 if text else 0.0

        return cast(TranscribeResult, {
            "text": text,
            "confidence": confidence,
            "ts": time.time(),
        })

    def close(self) -> None:
        # pywhispercpp Model has no explicit close; let GC handle it
        self._model = None
        self._closed = True

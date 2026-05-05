"""Vosk STT engine adapter.

Wraps Vosk's KaldiRecognizer in the utterance-level adapter contract. A fresh
KaldiRecognizer is built per `transcribe()` call — Vosk recognizers are stateful
and we want one utterance per recognizer.
"""
import json
import time
from typing import cast

from .base import TranscribeResult


class VoskEngine:
    name = "vosk"
    sample_rate = 16000

    def __init__(self, model_path: str) -> None:
        # Lazy-import vosk so test mocks can patch at module-level
        from vosk import Model
        self._model = Model(model_path)
        self._closed = False

    def transcribe(self, audio_pcm: bytes) -> TranscribeResult:
        if self._closed:
            raise RuntimeError("VoskEngine is closed")
        if not audio_pcm:
            return cast(TranscribeResult, {"text": "", "confidence": 0.0, "ts": time.time()})
        from vosk import KaldiRecognizer
        rec = KaldiRecognizer(self._model, self.sample_rate)
        # Per-utterance: feed all audio, then read final.
        rec.AcceptWaveform(audio_pcm)
        result = json.loads(rec.FinalResult())
        text = result.get("text", "").strip()
        # Compute mean word-level confidence if available; fall back to a simple
        # heuristic: 1.0 if any text, else 0.0.
        words = result.get("result", [])
        if words:
            confs = [w.get("conf", 0.0) for w in words if "conf" in w]
            confidence = sum(confs) / len(confs) if confs else (1.0 if text else 0.0)
        else:
            confidence = 1.0 if text else 0.0
        return cast(TranscribeResult, {
            "text": text,
            "confidence": float(confidence),
            "ts": time.time(),
        })

    def close(self) -> None:
        # Vosk Model has no explicit close; rely on GC. Mark closed to make calls fail loudly.
        self._closed = True

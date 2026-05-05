"""STT engine adapter contract.

All STT engines (Vosk, whisper.cpp, whisper-RKNN) implement this protocol.
The adapter is utterance-level: feed a complete utterance's worth of PCM
audio to `transcribe()`, get back text + confidence + timestamp.
"""
from typing import Protocol, runtime_checkable, TypedDict


class TranscribeResult(TypedDict):
    text: str           # final transcribed text, stripped, may be empty
    confidence: float   # 0.0 (no signal) to 1.0 (high confidence)
    ts: float           # epoch seconds when transcription completed


@runtime_checkable
class STTEngine(Protocol):
    name: str           # engine identifier, e.g. "vosk", "whisper-bindings"
    sample_rate: int    # required input sample rate (Hz), e.g. 16000

    def transcribe(self, audio_pcm: bytes) -> TranscribeResult:
        """Transcribe a complete utterance.

        Args:
            audio_pcm: signed 16-bit little-endian PCM at self.sample_rate Hz, mono.
                       May be empty bytes (returns empty text, confidence 0.0).

        Returns:
            TranscribeResult dict.

        Raises:
            RuntimeError if the engine is not loaded or the input is malformed.
        """
        ...

    def close(self) -> None:
        """Release engine resources. Idempotent — safe to call multiple times."""
        ...

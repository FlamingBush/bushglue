"""TTS engine adapter contract.

All TTS engines (espeak-ng, Piper) implement this protocol. The adapter is
utterance-level: feed text, get back the synthesized audio's PCM bytes plus
the sample rate that PCM is at. Downstream sox effects are appended by the
caller (bush-tts main loop or Discord client).
"""
from typing import Protocol, runtime_checkable, TypedDict


class SynthesisResult(TypedDict):
    audio_pcm: bytes      # raw int16 LE PCM bytes, mono
    sample_rate: int      # Hz of the audio_pcm
    ts: float             # epoch seconds when synthesis completed


@runtime_checkable
class TTSEngine(Protocol):
    name: str             # engine identifier, e.g. "espeak", "piper"

    def synthesize(self, text: str) -> SynthesisResult:
        """Synthesize text to a complete audio buffer.

        Args:
            text: arbitrary UTF-8 string. Empty string returns empty audio.

        Returns:
            SynthesisResult dict with raw PCM, sample_rate, and ts.

        Raises:
            RuntimeError if the engine is closed or synthesis fails irrecoverably.
        """
        ...

    def close(self) -> None:
        """Release engine resources. Idempotent."""
        ...

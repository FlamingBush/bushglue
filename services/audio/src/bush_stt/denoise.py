"""RNNoise denoiser wrapper.

Operates at 48 kHz mono int16 LE PCM in 480-sample frames (10 ms each).
The bush_stt main loop calls feed() with arbitrary-length 48 kHz audio,
gets back denoised 48 kHz audio, then downsamples to 16 kHz once via
soxr for VAD + recognizer.

Per D4 (eng review): RNNoise is the spec-locked denoiser. Capture happens
at 48 kHz; this module is the in-place noise filter at the top of the
chain.

Module-level loader is injected for testability — tests pass a stub that
echoes input through unchanged.
"""
from __future__ import annotations

import os
from typing import Callable, Optional

import numpy as np

# RNNoise frame size at 48 kHz
FRAME_SAMPLES = 480           # 10 ms @ 48 kHz
FRAME_BYTES = FRAME_SAMPLES * 2  # int16 LE
SAMPLE_RATE = 48000


def _env_bool(name: str, default: bool) -> bool:
    v = os.environ.get(name, "").strip().lower()
    if not v:
        return default
    return v not in ("0", "false", "no")


# Bypass flag for emergency disable / dev environments
DEFAULT_ENABLED = _env_bool("BUSH_RNNOISE_ENABLED", True)


def log(msg: str) -> None:
    print(f"[denoise] {msg}", flush=True)


def _default_load_filter():
    """Load pyrnnoise's RNNoise filter. Returns an object with `.process(int16_array)` method."""
    try:
        from pyrnnoise import RNNoise
    except ImportError as e:
        raise RuntimeError(
            "pyrnnoise not installed. Run: uv add pyrnnoise   "
            "(or pip install pyrnnoise; ARM64 wheels available as of 0.x)."
        ) from e
    return RNNoise()


class RnnoiseFilter:
    """Stateful 48 kHz RNNoise filter. NOT thread-safe — call from one thread only."""

    def __init__(
        self,
        *,
        enabled: bool = DEFAULT_ENABLED,
        loader: Optional[Callable] = None,
    ):
        self.enabled = enabled
        self._partial = bytearray()  # accumulator for frame-aligned 480-sample chunks
        self._closed = False
        if not self.enabled:
            log("disabled (BUSH_RNNOISE_ENABLED=0); pass-through mode")
            self._filter = None
            return
        loader = loader or _default_load_filter
        self._filter = loader()
        log("loaded; 48 kHz / 480-sample frames")

    def process(self, audio_chunk: bytes) -> bytes:
        """Feed arbitrary-length 48 kHz int16 LE PCM, return denoised PCM.

        Frame-aligns internally: any tail < 480 samples is buffered for the next
        call. Calling flush() drains the partial frame (zero-padded).
        """
        if self._closed:
            raise RuntimeError("RnnoiseFilter is closed")
        if not self.enabled:
            return audio_chunk
        self._partial.extend(audio_chunk)
        out = bytearray()
        while len(self._partial) >= FRAME_BYTES:
            frame = bytes(self._partial[:FRAME_BYTES])
            del self._partial[:FRAME_BYTES]
            out.extend(self._process_frame(frame))
        return bytes(out)

    def flush(self) -> bytes:
        """Drain any partial frame (zero-pad to 480 samples). Use at end of stream."""
        if self._closed or not self.enabled or not self._partial:
            return b""
        pad = FRAME_BYTES - len(self._partial)
        frame = bytes(self._partial) + b"\x00" * pad
        self._partial.clear()
        return self._process_frame(frame)

    def reset(self) -> None:
        """Clear the partial-frame buffer. Filter state inside RNNoise is not reset
        (the filter is causal; resetting would create an audible discontinuity).
        Call this only on stream-boundary events (mic restart, device swap)."""
        self._partial.clear()

    def close(self) -> None:
        self._partial.clear()
        self._filter = None
        self._closed = True

    # ── inner ────────────────────────────────────────────────────────────────

    def _process_frame(self, frame_bytes: bytes) -> bytes:
        """Run RNNoise on one 480-sample frame; return denoised int16 LE bytes."""
        arr = np.frombuffer(frame_bytes, dtype=np.int16)
        # pyrnnoise.RNNoise.process_frame takes/returns int16 numpy arrays.
        # If a different binding is used (e.g. a stub returning float32), accept it.
        out = self._filter.process_frame(arr) if hasattr(self._filter, "process_frame") else self._filter(arr)
        if out is None:
            # Stub returned None → echo input through unchanged
            return frame_bytes
        if isinstance(out, np.ndarray):
            if out.dtype != np.int16:
                out = np.clip(out, -1.0, 1.0) if out.dtype.kind == "f" else out
                out = (out * 32767.0).astype(np.int16) if out.dtype.kind == "f" else out.astype(np.int16)
            return out.tobytes()
        if isinstance(out, (bytes, bytearray)):
            return bytes(out)
        # Unknown return type — fall back to input
        return frame_bytes

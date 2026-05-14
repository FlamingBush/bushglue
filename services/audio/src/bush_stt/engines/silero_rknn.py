"""Silero VAD on the RK3588 NPU via rknn-toolkit-lite2.

Mirrors the public surface that bush_stt.vad expects from silero_vad's
load_silero_vad():

    model(audio_tensor_or_array, sample_rate) -> tensor or scalar in [0, 1]
    model.reset_states()

Hidden state is managed explicitly (shape [2, 1, 128] for Silero v5), so
there's no autograd surface and no per-frame leak class — the whole reason
torch.inference_mode is needed for the CPU path doesn't apply here.

The .rknn artifact lives at models/rknn/silero_vad.rknn (git-lfs). Path can
be overridden with BUSH_RKNN_MODELS_DIR.
"""
from __future__ import annotations

import os
import pathlib
from typing import Any

import numpy as np


SAMPLE_RATE = 16000
FRAME_SAMPLES = 512  # Silero's required frame size at 16 kHz
STATE_SHAPE = (2, 1, 128)


def _models_dir() -> pathlib.Path:
    override = os.environ.get("BUSH_RKNN_MODELS_DIR")
    if override:
        return pathlib.Path(override)
    # services/audio/src/bush_stt/engines/silero_rknn.py → repo root is parents[5]
    return pathlib.Path(__file__).resolve().parents[5] / "models" / "rknn"


class RknnSileroVad:
    """Callable Silero-VAD replacement backed by the RKNPU."""

    def __init__(self, model_path: str | None = None, core_mask: int | None = None) -> None:
        from rknnlite.api import RKNNLite

        path = model_path or str(_models_dir() / "silero_vad.rknn")
        if not pathlib.Path(path).exists():
            raise FileNotFoundError(
                f"Silero RKNN model not found at {path}. "
                f"Run tools/convert-rknn.py --silero on an x86_64 Linux host."
            )

        self._rknn = RKNNLite()
        ret = self._rknn.load_rknn(path)
        if ret != 0:
            raise RuntimeError(f"load_rknn({path}) failed: {ret}")

        # Pin VAD to core 0 so Whisper (when on NPU) can use cores 1+2.
        # RKNN_NPU_CORE_0 == 1; fall back to AUTO if the constant differs.
        if core_mask is None:
            core_mask = getattr(RKNNLite, "NPU_CORE_0", 1)
        ret = self._rknn.init_runtime(core_mask=core_mask)
        if ret != 0:
            raise RuntimeError(f"init_runtime failed: {ret}")

        self._state = np.zeros(STATE_SHAPE, dtype=np.float32)
        self._sr = np.array(SAMPLE_RATE, dtype=np.int64)

    def __call__(self, audio: Any, sample_rate: int) -> float:
        if sample_rate != SAMPLE_RATE:
            raise ValueError(f"RknnSileroVad only supports {SAMPLE_RATE} Hz, got {sample_rate}")

        # Accept torch tensors, numpy arrays, or anything with __array__.
        arr = np.asarray(audio, dtype=np.float32)
        if arr.shape == (FRAME_SAMPLES,):
            arr = arr.reshape(1, FRAME_SAMPLES)
        elif arr.shape != (1, FRAME_SAMPLES):
            raise ValueError(f"expected frame of {FRAME_SAMPLES} samples, got shape {arr.shape}")

        outputs = self._rknn.inference(inputs=[arr, self._state, self._sr])
        prob = float(np.asarray(outputs[0]).reshape(-1)[0])
        # Update hidden state for the next frame.
        self._state = np.asarray(outputs[1], dtype=np.float32).reshape(STATE_SHAPE)
        return prob

    def reset_states(self) -> None:
        self._state.fill(0.0)

    def close(self) -> None:
        if self._rknn is not None:
            try:
                self._rknn.release()
            except Exception:
                pass
            self._rknn = None


def load_silero_vad_rknn() -> RknnSileroVad:
    """Factory matching the silero_vad.load_silero_vad() naming pattern."""
    return RknnSileroVad()

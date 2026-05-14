"""Whisper base.en on the RK3588 NPU via rknn-toolkit-lite2.

Ported from airockchip/rknn_model_zoo examples/whisper/python/whisper.py
(rev pinned in tools/convert-rknn.py). Uses scipy/numpy in place of torch
so this engine doesn't drag torch onto the audio service hot path.

Two .rknn artifacts and two text artifacts must exist in models/rknn/:
  whisper_base_en_encoder.rknn
  whisper_base_en_decoder.rknn
  whisper_mel_80_filters.txt
  whisper_vocab_en.txt

All produced by tools/convert-rknn.py --whisper on an x86 host.

Rockchip's port hardcodes a 20-second chunk window and a fixed 12-token
rolling decoder input shape. Utterances longer than 20 s are truncated;
real-world bushglue utterances are ~5-10 s after VAD endpointing, so this
is fine.
"""
from __future__ import annotations

import os
import pathlib
import time
from typing import cast

import numpy as np

from .base import TranscribeResult


# ── Whisper constants (match Rockchip's port) ─────────────────────────────
SAMPLE_RATE = 16000
N_FFT = 400         # 25 ms window
HOP_LENGTH = 160    # 10 ms hop
CHUNK_LENGTH = 20   # seconds
N_SAMPLES = CHUNK_LENGTH * SAMPLE_RATE
MEL_FRAMES = CHUNK_LENGTH * 100  # 2000
N_MELS = 80

# Token ids for base.en
SOT = 50258
TASK_TRANSCRIBE = 50359
NO_TIMESTAMPS = 50363
LANG_EN = 50259
EOT = 50257
TIMESTAMP_BEGIN = 50364

# Decoder rolling window size (Rockchip's port: fixed at 12).
WINDOW_TOKENS = 12
MAX_NEW_TOKENS = 224  # whisper's max output length; we stop on EOT


def log(msg: str) -> None:
    print(f"[whisper-rknn] {msg}", flush=True)


def _models_dir() -> pathlib.Path:
    override = os.environ.get("BUSH_RKNN_MODELS_DIR")
    if override:
        return pathlib.Path(override)
    return pathlib.Path(__file__).resolve().parents[5] / "models" / "rknn"


def _read_vocab(path: pathlib.Path) -> dict[int, str]:
    vocab: dict[int, str] = {}
    with path.open() as f:
        for line in f:
            parts = line.rstrip("\n").split(" ", 1)
            key = parts[0]
            val = parts[1] if len(parts) > 1 else ""
            try:
                vocab[int(key)] = val
            except ValueError:
                continue
    return vocab


def _read_mel_filters(path: pathlib.Path) -> np.ndarray:
    # 80 mel bins × (N_FFT/2 + 1) = 80 × 201
    return np.loadtxt(path, dtype=np.float32).reshape(N_MELS, N_FFT // 2 + 1)


def _log_mel_spectrogram(audio: np.ndarray, filters: np.ndarray) -> np.ndarray:
    """Whisper-style 80-bin log-mel. audio: float32 1-D in [-1, 1]."""
    from scipy.signal import stft as scipy_stft

    # scipy returns (f, t, Z); use Hann window matching torch.stft defaults.
    # noverlap = N_FFT - HOP_LENGTH; nperseg = N_FFT; padded=False; boundary=None
    # match torch.stft(center=True) by mirror-padding manually.
    pad = N_FFT // 2
    a = np.pad(audio, (pad, pad), mode="reflect")
    _, _, z = scipy_stft(
        a,
        fs=SAMPLE_RATE,
        window="hann",
        nperseg=N_FFT,
        noverlap=N_FFT - HOP_LENGTH,
        nfft=N_FFT,
        boundary=None,
        padded=False,
        return_onesided=True,
    )
    # scipy normalizes by sum(window); torch does not. Undo:
    z = z * np.sum(np.hanning(N_FFT))
    magnitudes = (np.abs(z[..., :-1])) ** 2  # drop last frame to match reference
    mel = filters @ magnitudes
    log_spec = np.log10(np.maximum(mel, 1e-10))
    log_spec = np.maximum(log_spec, log_spec.max() - 8.0)
    log_spec = (log_spec + 4.0) / 4.0
    return log_spec.astype(np.float32)


def _pad_or_trim_mel(mel: np.ndarray) -> np.ndarray:
    out = np.zeros((N_MELS, MEL_FRAMES), dtype=np.float32)
    width = min(mel.shape[1], MEL_FRAMES)
    out[:, :width] = mel[:, :width]
    return out


class WhisperRknnEngine:
    name = "whisper-rknn"
    sample_rate = SAMPLE_RATE

    def __init__(self, models_dir: str | None = None) -> None:
        from rknnlite.api import RKNNLite

        d = pathlib.Path(models_dir) if models_dir else _models_dir()
        enc_path = d / "whisper_base_en_encoder.rknn"
        dec_path = d / "whisper_base_en_decoder.rknn"
        filt_path = d / "whisper_mel_80_filters.txt"
        vocab_path = d / "whisper_vocab_en.txt"
        for p in (enc_path, dec_path, filt_path, vocab_path):
            if not p.exists():
                raise FileNotFoundError(
                    f"missing Whisper RKNN artifact: {p}. "
                    f"Run tools/convert-rknn.py --whisper on x86_64 Linux."
                )

        self._filters = _read_mel_filters(filt_path)
        self._vocab = _read_vocab(vocab_path)

        # Gang cores 1+2 for the encoder; the decoder rides one core.
        core_all = getattr(RKNNLite, "NPU_CORE_0_1_2", 7)
        core_dec = getattr(RKNNLite, "NPU_CORE_1", 2)

        log(f"loading encoder: {enc_path}")
        t0 = time.time()
        self._enc = RKNNLite()
        if self._enc.load_rknn(str(enc_path)) != 0:
            raise RuntimeError(f"encoder load_rknn failed")
        if self._enc.init_runtime(core_mask=core_all) != 0:
            raise RuntimeError("encoder init_runtime failed")
        log(f"encoder loaded in {(time.time()-t0)*1000:.0f} ms")

        log(f"loading decoder: {dec_path}")
        t0 = time.time()
        self._dec = RKNNLite()
        if self._dec.load_rknn(str(dec_path)) != 0:
            raise RuntimeError(f"decoder load_rknn failed")
        if self._dec.init_runtime(core_mask=core_dec) != 0:
            raise RuntimeError("decoder init_runtime failed")
        log(f"decoder loaded in {(time.time()-t0)*1000:.0f} ms")

        self._closed = False

        # Warmup: 250 ms silence
        warmup = np.zeros(int(SAMPLE_RATE * 0.25), dtype=np.float32)
        t0 = time.time()
        try:
            self._run(warmup)
            log(f"warmup complete in {(time.time()-t0)*1000:.0f} ms")
        except Exception as e:
            log(f"warmup failed (non-fatal): {e}")

    def transcribe(self, audio_pcm: bytes) -> TranscribeResult:
        if self._closed:
            raise RuntimeError("WhisperRknnEngine is closed")
        if not audio_pcm:
            return cast(TranscribeResult, {"text": "", "confidence": 0.0, "ts": time.time()})
        arr = np.frombuffer(audio_pcm, dtype=np.int16).astype(np.float32) / 32768.0
        text = self._run(arr)
        return cast(TranscribeResult, {
            "text": text,
            "confidence": 1.0 if text else 0.0,
            "ts": time.time(),
        })

    def close(self) -> None:
        for h in (getattr(self, "_enc", None), getattr(self, "_dec", None)):
            if h is not None:
                try:
                    h.release()
                except Exception:
                    pass
        self._enc = None
        self._dec = None
        self._closed = True

    # ── internals ────────────────────────────────────────────────────────
    def _run(self, audio: np.ndarray) -> str:
        # Truncate to 20 s; bushglue utterances are already well under.
        if audio.size > N_SAMPLES:
            audio = audio[:N_SAMPLES]
        mel = _log_mel_spectrogram(audio, self._filters)
        mel = _pad_or_trim_mel(mel)
        # Encoder expects shape (1, 80, 2000)
        enc_in = mel.reshape(1, N_MELS, MEL_FRAMES)
        enc_out = self._enc.inference(inputs=[enc_in])[0]
        return self._decode(enc_out)

    def _decode(self, enc_out: np.ndarray) -> str:
        # Rockchip's port uses a fixed 12-token rolling window. Seed with the
        # standard English transcribe prefix repeated to fill the window.
        prefix = [SOT, LANG_EN, TASK_TRANSCRIBE, NO_TIMESTAMPS]
        tokens = list(prefix) * (WINDOW_TOKENS // len(prefix))
        out_str = ""
        pop_id = WINDOW_TOKENS
        produced = 0

        while produced < MAX_NEW_TOKENS:
            tok_in = np.asarray([tokens], dtype=np.int64)
            dec_out = self._dec.inference(inputs=[tok_in, enc_out])[0]
            next_token = int(dec_out[0, -1].argmax())
            tokens.append(next_token)
            produced += 1

            if next_token == EOT:
                tokens.pop(-1)
                break
            if next_token > TIMESTAMP_BEGIN:
                continue
            if pop_id > 4:
                pop_id -= 1
            tokens.pop(pop_id)
            piece = self._vocab.get(next_token, "")
            out_str += piece

        # Whisper's BPE uses Ġ for word boundaries.
        return out_str.replace("Ġ", " ").replace("<|endoftext|>", "").strip()

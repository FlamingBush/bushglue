"""Music feature extraction for bush-cue.

Decode (ffmpeg) -> mono 22050 -> STFT (2048/512) -> per-frame features: RMS,
four band energies, spectral centroid (brightness), spectral flux (the onset
envelope). Onsets via an adaptive median+MAD peak-pick; tempo via autocorrelation
with a log-Gaussian prior; beats via a DP tracker (Ellis 2007).

STFT conventions match bush_stt/engines/whisper_rknn.py (periodic Hann,
reflect-padded, unnormalized magnitude). numpy/scipy only -- no librosa/aubio.

Voice later swaps compute_features() for a speech profile filling the same
arrays; mapping/safety downstream are unchanged.
"""
from __future__ import annotations

import shutil
import subprocess
import sys

import numpy as np

SR = 22050
N_FFT = 2048
HOP = 512
FPS = SR / HOP  # ~43.07 feature frames/sec

# Band edges in Hz: sub-bass, bass, mid, high.
BANDS = ((20.0, 60.0), (60.0, 250.0), (250.0, 2000.0), (2000.0, SR / 2))


def log(msg: str) -> None:
    print(f"[bush-cue] {msg}", file=sys.stderr, flush=True)


def decode_to_mono(src: str, sr: int = SR) -> np.ndarray:
    """Decode any ffmpeg-readable file (or '-' for stdin) to mono float32 @ sr."""
    if not shutil.which("ffmpeg"):
        raise RuntimeError("ffmpeg not found -- install it (apt install ffmpeg)")
    inp = "pipe:0" if src == "-" else src
    cmd = ["ffmpeg", "-v", "error", "-i", inp, "-vn",
           "-f", "f32le", "-ac", "1", "-ar", str(sr), "pipe:1"]
    stdin = sys.stdin.buffer if src == "-" else subprocess.DEVNULL
    proc = subprocess.Popen(cmd, stdin=stdin,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    out, err = proc.communicate()
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg decode failed: {err.decode(errors='ignore')[:300]}")
    audio = np.frombuffer(out, dtype=np.float32).copy()
    if audio.size == 0:
        raise RuntimeError("decoded zero samples (empty/unreadable audio)")
    return audio


def _stft_mag(audio: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Return (freqs, magnitude[F, T]). Unnormalized, reflect-padded Hann."""
    from scipy.signal import stft

    pad = N_FFT // 2
    a = np.pad(audio, (pad, pad), mode="reflect")
    f, _, z = stft(a, fs=SR, window="hann", nperseg=N_FFT, noverlap=N_FFT - HOP,
                   nfft=N_FFT, boundary=None, padded=False, return_onesided=True)
    z = z * np.sum(np.hanning(N_FFT))  # undo scipy's window normalization
    return f, np.abs(z).astype(np.float32)


def compute_features(audio: np.ndarray) -> dict:
    """Per-frame music features. Returns dict of 1-D arrays aligned to `times`."""
    f, mag = _stft_mag(audio)
    power = mag ** 2
    n = mag.shape[1]
    times = np.arange(n) / FPS

    bands = np.empty((4, n), dtype=np.float32)
    for i, (lo, hi) in enumerate(BANDS):
        sel = (f >= lo) & (f < hi)
        bands[i] = power[sel].sum(axis=0)

    rms = np.sqrt(power.mean(axis=0) + 1e-12).astype(np.float32)
    centroid = ((f[:, None] * mag).sum(axis=0) / (mag.sum(axis=0) + 1e-9)).astype(np.float32)

    logmag = np.log1p(mag)
    flux = np.maximum(0.0, np.diff(logmag, axis=1)).sum(axis=0)
    flux = np.concatenate([[0.0], flux]).astype(np.float32)

    return {"times": times, "rms": rms, "bands": bands,
            "centroid": centroid, "flux": flux, "n_frames": n}


def detect_onsets(flux: np.ndarray, threshold: float = 1.5,
                  win_frames: int = 5, refractory_frames: int = 2) -> list[tuple[int, float]]:
    """Adaptive local-median + threshold*MAD peak-pick. Returns [(frame, strength)]."""
    n = len(flux)
    out: list[tuple[int, float]] = []
    last = -(10 ** 9)
    for t in range(1, n - 1):
        a, b = max(0, t - win_frames), min(n, t + win_frames + 1)
        seg = flux[a:b]
        med = float(np.median(seg))
        mad = float(np.median(np.abs(seg - med))) + 1e-9
        if flux[t] > med + threshold * mad and flux[t] >= flux[t - 1] and flux[t] >= flux[t + 1]:
            if t - last >= refractory_frames:
                out.append((t, (flux[t] - med) / mad))
                last = t
    return out


def estimate_tempo(flux: np.ndarray, min_bpm: float = 60.0, max_bpm: float = 200.0,
                   prior_bpm: float = 120.0, prior_width: float = 0.5) -> float:
    """Autocorrelation of the onset envelope, biased by a log-Gaussian tempo prior."""
    env = flux - flux.mean()
    if not np.any(env):
        return prior_bpm
    ac = np.correlate(env, env, mode="full")[len(env) - 1:]
    lags = np.arange(len(ac))
    with np.errstate(divide="ignore", invalid="ignore"):
        bpm = 60.0 * FPS / lags
    mask = (lags > 0) & (bpm >= min_bpm) & (bpm <= max_bpm)
    if not np.any(mask):
        return prior_bpm
    prior = np.exp(-0.5 * (np.log(np.where(mask, bpm, prior_bpm) / prior_bpm) / prior_width) ** 2)
    score = np.where(mask, ac * prior, -np.inf)
    return float(bpm[int(np.argmax(score))])


def track_beats(flux: np.ndarray, bpm: float, tightness: float = 100.0) -> list[int]:
    """DP beat tracker (Ellis 2007). Returns beat frame indices."""
    fpb = 60.0 * FPS / max(1e-6, bpm)
    n = len(flux)
    if fpb < 1.0 or n < 2:
        return []
    onset = np.maximum(flux - np.median(flux), 0.0)
    onset = onset / (onset.max() + 1e-9)
    cumscore = onset.copy()
    backlink = np.full(n, -1, dtype=int)
    lo, hi = int(np.floor(fpb * 0.5)), int(np.ceil(fpb * 2.0))
    for i in range(1, n):
        a, b = max(0, i - hi), max(0, i - lo)
        if b <= a:
            continue
        js = np.arange(a, b)
        scores = cumscore[js] - tightness * (np.log((i - js) / fpb)) ** 2
        k = int(np.argmax(scores))
        if scores[k] > 0:
            cumscore[i] = onset[i] + scores[k]
            backlink[i] = js[k]
    beats: list[int] = []
    b = int(np.argmax(cumscore))
    while b >= 0:
        beats.append(b)
        b = backlink[b]
    beats.reverse()
    return beats

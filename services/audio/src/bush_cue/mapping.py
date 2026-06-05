"""Map music features -> valve position waveform + flame pulse track.

Continuous channel: weighted band energy -> smoothed contour -> beat strokes ->
mapped into [pos_min, pos_max], resampled to rate_hz. Discrete channel: onsets/
beats -> flame pulses per the preset's flame_rules. Both pass through safety.
"""
from __future__ import annotations

import numpy as np

from . import features as F
from . import safety


def _smooth(x: np.ndarray, attack: float, release: float) -> np.ndarray:
    """Asymmetric one-pole follower; attack/release are time-constants in frames."""
    a_up = np.exp(-1.0 / max(1e-6, attack))
    a_dn = np.exp(-1.0 / max(1e-6, release))
    y = np.empty_like(x)
    prev = float(x[0]) if len(x) else 0.0
    for i, v in enumerate(x):
        a = a_up if v > prev else a_dn
        prev = a * prev + (1.0 - a) * v
        y[i] = prev
    return y


def _agc_ref(e: np.ndarray, win_frames: float) -> np.ndarray:
    """Decaying peak-hold reference for adaptive normalization."""
    a = np.exp(-1.0 / max(1.0, win_frames))
    out = np.empty_like(e)
    r = float(e.max()) * 0.5 if len(e) else 1.0
    for i, v in enumerate(e):
        r = max(float(v), a * r)
        out[i] = r
    return out


def _energy(feat: dict, knobs: dict) -> np.ndarray:
    """Normalized 0..1 energy contour per feature frame."""
    tilt = float(knobs["tone_tilt"])
    w = np.array([1 - tilt, 1 - tilt, 1 + tilt, 1 + tilt], dtype=np.float32)
    w = np.clip(w, 0.0, None)
    e = np.sqrt((feat["bands"] * w[:, None]).sum(axis=0) + 1e-12)

    gate = float(knobs["energy_gate"])
    e = np.maximum(0.0, e - gate * (e.max() + 1e-9))

    agc = knobs["agc"]
    if agc == "off":
        ref = np.percentile(e, 95) + 1e-9
    else:
        ref = _agc_ref(e, F.FPS * (8 if agc == "fast" else 30)) + 1e-9
    norm = np.clip((e / ref) * float(knobs["gain"]), 0.0, 1.0)

    c = float(knobs.get("compression", 0.0))
    if c > 0:
        norm = norm ** (1.0 - 0.7 * c)  # lift quiet sections toward the middle
    return np.clip(norm, 0.0, 1.0)


def _beat_strokes(n: int, beats: list[int], width_s: float = 0.12) -> np.ndarray:
    s = np.zeros(n, dtype=np.float32)
    half = max(1, int(width_s * F.FPS))
    bump = 0.5 * (1.0 - np.cos(np.linspace(0.0, 2.0 * np.pi, 2 * half)))
    for bf in beats:
        a, b = bf, min(n, bf + len(bump))
        if b > a:
            s[a:b] = np.maximum(s[a:b], bump[: b - a])
    return s


def _resample(x: np.ndarray, src_fps: float, rate_hz: int) -> np.ndarray:
    dur = len(x) / src_fps
    m = max(1, int(round(dur * rate_hz)))
    src_t = np.arange(len(x)) / src_fps
    dst_t = np.arange(m) / rate_hz
    return np.interp(dst_t, src_t, x).astype(np.float32)


def _flame(onsets: list[tuple[int, float]], beats: list[int],
           preset: dict, knobs: dict) -> list[dict]:
    chans = knobs["channels"]
    out: list[dict] = []
    for rule in preset.get("flame_rules", []):
        ch = rule["channel"]
        if ch not in chans:
            continue
        if rule["source"] == "beat":
            events = [(bf, 1.0) for bf in beats]
        else:
            events = onsets
        for frame, strength in events:
            if strength < rule["min_strength"]:
                continue
            ms = rule["ms_base"] + rule["ms_scale"] * min(strength, 4.0)
            out.append({"t": frame / F.FPS, "valve": ch, "ms": int(ms)})
    return out


def build(feat: dict, onsets: list[tuple[int, float]], beats: list[int],
          preset: dict, knobs: dict) -> dict:
    n = feat["n_frames"]
    contour = _energy(feat, knobs)

    ms = float(knobs["motion_smoothing"])
    contour = _smooth(contour, attack=(0.01 + ms * 0.5) * F.FPS,
                      release=(0.05 + ms * 1.5) * F.FPS)

    depth = float(knobs["stroke_depth"])
    if depth > 0 and "valve" in knobs["channels"]:
        contour = np.clip(contour + depth * _beat_strokes(n, beats), 0.0, 1.0)

    lo, hi = float(knobs["pos_min"]), float(knobs["pos_max"])
    pos = lo + contour * (hi - lo)
    if knobs["invert"]:
        pos = (lo + hi) - pos

    rate = int(knobs["rate_hz"])
    pos = _resample(pos, F.FPS, rate)
    pos = safety.clamp_valve(pos, lo, hi)

    flame = safety.filter_flame(_flame(onsets, beats, preset, knobs),
                                float(knobs["max_cue_rate"]))

    return {
        "valve": {"rate_hz": rate, "pos": [round(float(p), 4) for p in pos]},
        "flame": flame,
    }

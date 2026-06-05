"""Non-bypassable output safety for bush-cue.

The flame relay firmware does NOT bound burn time -- it takes `ms` raw and is
extend-only (overlapping pulses push the OFF deadline later, never shorter,
relay-control/code.py). So every burn-time limit lives HERE, in the publisher,
as hard code. Preset/knob values may only make these MORE conservative.

Applied both when building a cue sheet and re-asserted at play time.
"""
from __future__ import annotations

import numpy as np

# Valve travel never rests on the force-sensitive closed seat (the valve homes
# INTO it). Keep the floor above a margin and below the ceiling.
SEAT_MARGIN_MIN = 0.02

# Per-channel pulse limits (ms). Floors match the existing services; ceilings
# bound a single burst's open time regardless of feature value.
MS_FLOOR = {"poof": 40, "flare": 50, "bigjet": 100}
MS_CEIL = {"poof": 300, "flare": 300, "bigjet": 600}

# Rolling duty-cycle budget: summed flame open-time over the trailing window may
# not exceed this fraction of it. Excess cues are DROPPED (never queued).
DUTY_WINDOW_S = 10.0
DUTY_MAX_FRAC = 0.30

# The big reveal can't machine-gun the fuel/relay.
BIGJET_MIN_GAP_S = 4.0


def clamp_valve(pos: np.ndarray, pos_min: float, pos_max: float) -> np.ndarray:
    """Clip the position waveform to a safe [lo, hi] above the seat margin."""
    lo = max(SEAT_MARGIN_MIN, min(pos_min, pos_max - 0.01))
    hi = max(lo + 0.01, pos_max)
    return np.clip(pos, lo, hi)


def filter_flame(cues: list[dict], max_cue_rate: float) -> list[dict]:
    """Clamp ms, enforce per-channel refractory + bigjet gap + duty budget.

    Returns a new, time-sorted list of {t, valve, ms}. Cues that would violate a
    limit are dropped, not deferred -- a dense passage can't build a backlog. All
    comparisons are in integer milliseconds so the guarantee isn't fp-fragile.
    """
    refr_ms = int(1000.0 / max(0.5, max_cue_rate))
    gap_ms = int(BIGJET_MIN_GAP_S * 1000)
    win_ms = int(DUTY_WINDOW_S * 1000)
    budget_ms = int(DUTY_MAX_FRAC * DUTY_WINDOW_S * 1000)
    last: dict[str, int] = {}
    window: list[tuple[int, int]] = []  # (t_ms, ms) within the trailing window
    out: list[dict] = []
    for c in sorted(cues, key=lambda x: x["t"]):
        ch = c["valve"]
        if ch not in MS_CEIL:
            continue
        t_ms = int(round(float(c["t"]) * 1000))
        ms = int(max(MS_FLOOR[ch], min(MS_CEIL[ch], int(c["ms"]))))
        if t_ms - last.get(ch, -(10 ** 9)) < refr_ms:
            continue
        if ch == "bigjet" and t_ms - last.get("bigjet", -(10 ** 9)) < gap_ms:
            continue
        window = [(tt, mm) for tt, mm in window if tt > t_ms - win_ms]
        if sum(mm for _, mm in window) + ms > budget_ms:
            continue
        out.append({"t": round(t_ms / 1000.0, 3), "valve": ch, "ms": ms})
        last[ch] = t_ms
        window.append((t_ms, ms))
    return out

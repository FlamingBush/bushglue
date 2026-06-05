"""Safety layer is the only thing standing between a feature glitch and the fuel
(the flame firmware has no burn-time ceiling). Test the hard limits directly."""
from __future__ import annotations

import numpy as np

from bush_cue import safety


def test_ms_ceiling_and_floor():
    cues = [{"t": 0.0, "valve": "poof", "ms": 9999},
            {"t": 10.0, "valve": "flare", "ms": 1}]
    out = safety.filter_flame(cues, max_cue_rate=6.0)
    assert out[0]["ms"] == safety.MS_CEIL["poof"]
    assert out[1]["ms"] == safety.MS_FLOOR["flare"]


def test_refractory_drops_dense_cues():
    # 50 poofs in one second, max_cue_rate 6 -> refractory ~167ms -> ~6 survive
    cues = [{"t": i * 0.02, "valve": "poof", "ms": 100} for i in range(50)]
    out = safety.filter_flame(cues, max_cue_rate=6.0)
    assert len(out) <= 7
    gaps = np.diff([c["t"] for c in out])
    assert (gaps >= 1.0 / 6.0 - 1e-6).all()


def test_bigjet_min_gap():
    cues = [{"t": t, "valve": "bigjet", "ms": 300} for t in (0.0, 1.0, 2.0, 5.0)]
    out = safety.filter_flame(cues, max_cue_rate=6.0)
    ts = [c["t"] for c in out]
    assert ts == [0.0, 5.0]  # 1.0 and 2.0 dropped by the 4s min gap


def test_duty_budget_suppresses_sustained_firing():
    # poof every 200ms at 300ms each = 1.5s open/s, far over the 30% budget
    cues = [{"t": i * 0.2, "valve": "poof", "ms": 300} for i in range(100)]
    out = safety.filter_flame(cues, max_cue_rate=12.0)
    budget_ms = int(safety.DUTY_MAX_FRAC * safety.DUTY_WINDOW_S * 1000)
    win_ms = int(safety.DUTY_WINDOW_S * 1000)
    # over any trailing window, summed open time stays within the budget
    for c in out:
        ct = round(c["t"] * 1000)
        open_ms = sum(x["ms"] for x in out if ct - win_ms < round(x["t"] * 1000) <= ct)
        assert open_ms <= budget_ms


def test_clamp_valve_keeps_off_the_seat():
    pos = np.array([-0.5, 0.0, 0.5, 1.0, 2.0])
    out = safety.clamp_valve(pos, pos_min=0.0, pos_max=0.85)
    assert out.min() >= safety.SEAT_MARGIN_MIN
    assert out.max() <= 0.85


def test_clamp_valve_rejects_inverted_range():
    out = safety.clamp_valve(np.array([0.5]), pos_min=0.9, pos_max=0.1)
    assert np.isfinite(out).all()

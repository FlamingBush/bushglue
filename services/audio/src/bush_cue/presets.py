"""Presets + the targeted knob surface for bush-cue.

Two knob categories the operator tweaks (analyze flags):
  sensitivity:  gain, onset_threshold, energy_gate, tone_tilt, agc
  range/style:  pos_min/pos_max, stroke_depth, motion_smoothing, tempo_lock,
                tempo_multiplier, max_cue_rate, channels, invert, rate_hz

Everything else (attack/release curves, flame assignment) is baked into a preset.
A preset = knob overrides on BASE + flame_rules describing how onsets/beats become
flame pulses.
"""
from __future__ import annotations

BASE = {
    # sensitivity
    "gain": 1.0,
    "onset_threshold": 1.5,
    "energy_gate": 0.03,
    "tone_tilt": 0.0,          # -1 bass .. +1 treble
    "agc": "slow",             # off | slow | fast
    # range / style
    "pos_min": 0.05,
    "pos_max": 0.85,
    "stroke_depth": 0.4,
    "motion_smoothing": 0.3,   # 0 snappy .. 1 legato
    "tempo_lock": False,
    "tempo_multiplier": 1.0,   # 0.5 | 1 | 2
    "max_cue_rate": 6.0,
    "invert": False,
    "rate_hz": 30,
    "channels": ["valve", "poof", "flare", "bigjet"],
    # preset-internal (not exposed as primary knobs)
    "compression": 0.4,
}

PRESETS = {
    "swell": {
        "knobs": {
            "stroke_depth": 0.0,
            "motion_smoothing": 0.7,
            "max_cue_rate": 3.0,
            "channels": ["valve"],
            "compression": 0.6,
        },
        "flame_rules": [],
    },
    "pulse": {
        "knobs": {
            "tone_tilt": -0.4,
            "stroke_depth": 0.55,
            "motion_smoothing": 0.2,
            "channels": ["valve", "poof", "flare"],
            "compression": 0.35,
        },
        "flame_rules": [
            {"channel": "poof", "source": "beat", "min_strength": 0.0,
             "ms_base": 120, "ms_scale": 40},
            {"channel": "flare", "source": "onset", "min_strength": 2.5,
             "ms_base": 90, "ms_scale": 40},
        ],
    },
    "drama": {
        "knobs": {
            "stroke_depth": 0.2,
            "motion_smoothing": 0.85,
            "max_cue_rate": 2.0,
            "channels": ["valve", "bigjet"],
            "compression": 0.2,
        },
        "flame_rules": [
            {"channel": "bigjet", "source": "onset", "min_strength": 3.0,
             "ms_base": 300, "ms_scale": 80},
        ],
    },
}


def resolve(preset_name: str, overrides: dict) -> tuple[dict, dict]:
    """Return (knobs, preset). overrides are CLI knob flags (only those given)."""
    if preset_name not in PRESETS:
        raise ValueError(f"unknown preset {preset_name!r} (have: {', '.join(PRESETS)})")
    preset = PRESETS[preset_name]
    knobs = dict(BASE)
    knobs.update(preset.get("knobs", {}))
    knobs.update({k: v for k, v in overrides.items() if v is not None})
    return knobs, preset

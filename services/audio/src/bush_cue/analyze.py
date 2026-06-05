"""`bush-cue analyze` -- audio file -> cue sheet. Pure CPU, no MQTT."""
from __future__ import annotations

import argparse

from . import cuesheet, features, mapping, presets
from .features import log


def add_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("file", help="audio file, or '-' for stdin (any ffmpeg format)")
    p.add_argument("--preset", default="pulse", help=", ".join(presets.PRESETS))
    p.add_argument("-o", "--out", default="-", help="cue sheet path, or '-' for stdout")
    # sensitivity
    p.add_argument("--gain", type=float)
    p.add_argument("--onset-threshold", type=float, dest="onset_threshold")
    p.add_argument("--energy-gate", type=float, dest="energy_gate")
    p.add_argument("--tone-tilt", type=float, dest="tone_tilt")
    p.add_argument("--agc", choices=["off", "slow", "fast"])
    # range / style
    p.add_argument("--pos-min", type=float, dest="pos_min")
    p.add_argument("--pos-max", type=float, dest="pos_max")
    p.add_argument("--stroke-depth", type=float, dest="stroke_depth")
    p.add_argument("--motion-smoothing", type=float, dest="motion_smoothing")
    p.add_argument("--tempo-lock", action=argparse.BooleanOptionalAction, dest="tempo_lock")
    p.add_argument("--tempo-multiplier", type=float, dest="tempo_multiplier")
    p.add_argument("--max-cue-rate", type=float, dest="max_cue_rate")
    p.add_argument("--invert", action=argparse.BooleanOptionalAction)
    p.add_argument("--rate-hz", type=int, dest="rate_hz")
    p.add_argument("--channels", help="comma list: valve,poof,flare,bigjet")


_KNOB_KEYS = ("gain", "onset_threshold", "energy_gate", "tone_tilt", "agc",
              "pos_min", "pos_max", "stroke_depth", "motion_smoothing",
              "tempo_lock", "tempo_multiplier", "max_cue_rate", "invert", "rate_hz")


def run(args: argparse.Namespace) -> int:
    overrides = {k: getattr(args, k) for k in _KNOB_KEYS}
    if args.channels:
        overrides["channels"] = [c.strip() for c in args.channels.split(",") if c.strip()]
    knobs, preset = presets.resolve(args.preset, overrides)

    log(f"decoding {args.file} ...")
    audio = features.decode_to_mono(args.file)
    dur = len(audio) / features.SR
    log(f"{dur:.1f}s @ {features.SR} Hz; extracting features ...")

    feat = features.compute_features(audio)
    onsets = features.detect_onsets(feat["flux"], threshold=knobs["onset_threshold"])
    bpm = features.estimate_tempo(feat["flux"]) * float(knobs["tempo_multiplier"])
    beats = features.track_beats(feat["flux"], bpm)
    log(f"tempo {bpm:.1f} BPM, {len(beats)} beats, {len(onsets)} onsets")

    out = mapping.build(feat, onsets, beats, preset, knobs)
    sheet = {
        "version": cuesheet.VERSION,
        "duration_s": round(dur, 2),
        "tempo_bpm": round(bpm, 1),
        "preset": args.preset,
        "knobs": knobs,
        "valve": out["valve"],
        "flame": out["flame"],
    }
    cuesheet.write(sheet, args.out)
    log(f"wrote {len(out['valve']['pos'])} valve samples "
        f"@ {out['valve']['rate_hz']} Hz, {len(out['flame'])} flame cues -> {args.out}")
    return 0

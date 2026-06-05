"""Cue sheet read/write -- the portable contract between analyze and the players.

  {
    "version": 1, "duration_s": 214.0, "tempo_bpm": 124.0, "preset": "pulse",
    "knobs": {...resolved values...},
    "valve": {"rate_hz": 30, "pos": [0.05, 0.07, ...]},   # 0..1 fraction, dense
    "flame": [{"t": 12.40, "valve": "poof", "ms": 140}, ...]
  }

Produced once on the odroid (Python); played by either the odroid or the Android
app. There is only one analyzer, so no golden-vector parity burden.
"""
from __future__ import annotations

import json
import sys

VERSION = 1


def write(sheet: dict, out: str) -> None:
    text = json.dumps(sheet, separators=(",", ":"))
    if out == "-":
        sys.stdout.write(text)
        sys.stdout.write("\n")
    else:
        with open(out, "w") as f:
            f.write(text)


def read(path: str) -> dict:
    with open(path) as f:
        sheet = json.load(f)
    if sheet.get("version") != VERSION:
        raise ValueError(f"unsupported cue sheet version {sheet.get('version')}")
    return sheet

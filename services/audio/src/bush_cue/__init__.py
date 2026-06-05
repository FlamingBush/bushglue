"""bush-cue -- turn audio into synchronized valve + flame cues.

  bush-cue analyze <file|-> --preset P [knobs] -o sheet.json
  bush-cue play <sheet.json> [--dry-run] [--no-flame]

`analyze` runs anywhere with ffmpeg (the odroid hosts it as an SSH conversion
"API"). `play` runs on the odroid (audio out + MQTT to the valve bridge / flame).
"""
from __future__ import annotations

import argparse
import sys


def main() -> int:
    ap = argparse.ArgumentParser(prog="bush-cue", description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)

    from . import analyze
    analyze.add_args(sub.add_parser("analyze", help="audio file -> cue sheet"))

    play_p = sub.add_parser("play", help="play a cue sheet (odroid)")
    play_p.add_argument("sheet")
    play_p.add_argument("--audio", help="audio file to play in sync (any ffmpeg format)")
    play_p.add_argument("--dry-run", action="store_true")
    play_p.add_argument("--no-flame", action="store_true")
    play_p.add_argument("--latency-lead-ms", type=int, default=120)

    args = ap.parse_args()
    if args.cmd == "analyze":
        return analyze.run(args)
    if args.cmd == "play":
        from . import play
        return play.run(args)
    ap.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""
Synthesize a phrase to WAV (or use an existing WAV) and inject it into the
Bush pipeline via stt-file-service.py.

Generated WAVs are saved to WAVS_DIR (default: ~/wavs/) for future replay.

Usage:
    inject.py --phrase "what is the meaning of fire"
    inject.py --file ~/wavs/what_is_the_meaning_of_fire.wav
    inject.py --phrase "consuming flame" --delay 2 --log runs/run1.jsonl
"""
import argparse
import os
import re
import subprocess
import sys
from pathlib import Path

WAVS_DIR = Path(os.environ.get("WAVS_DIR", Path.home() / "wavs"))
STT_DIR = os.environ.get("STT_DIR", "/mnt/c/Users/EB/speech-to-text")
STT_MODEL = os.environ.get("STT_MODEL", f"{STT_DIR}/models/en-us")
SCRIPTS_DIR = Path(__file__).parent


def phrase_to_filename(phrase: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", phrase.lower().strip()).strip("_")
    return f"{slug}.wav"


def synthesize(phrase: str, out_path: Path) -> None:
    espeak = subprocess.Popen(
        ["espeak-ng", "-v", "en-us", "-s", "130", "--stdout", phrase],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    subprocess.run(
        ["sox", "-t", "wav", "-", "-r", "16000", "-c", "1", "-b", "16", str(out_path)],
        stdin=espeak.stdout,
        check=True,
    )
    espeak.stdout.close()
    espeak.wait()


def main():
    parser = argparse.ArgumentParser(
        description="Synthesize or load a WAV and inject into the Bush pipeline."
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--phrase", help="Text to synthesize and inject")
    group.add_argument("--file", help="Path to existing WAV file")
    parser.add_argument("--delay", type=float, default=0.0,
                        help="Pause between utterances (passed to stt-file-service)")
    parser.add_argument("--log", help="Append utterances as JSONL to this file")
    args = parser.parse_args()

    if args.phrase:
        WAVS_DIR.mkdir(parents=True, exist_ok=True)
        wav = WAVS_DIR / phrase_to_filename(args.phrase)
        if not wav.exists():
            print(f"[inject] Synthesizing → {wav}")
            synthesize(args.phrase, wav)
        else:
            print(f"[inject] Using cached  → {wav}")
    else:
        wav = Path(args.file).expanduser()
        if not wav.exists():
            print(f"[inject] File not found: {wav}", file=sys.stderr)
            sys.exit(1)

    cmd = [sys.executable, str(SCRIPTS_DIR / "stt-file-service.py"), "--file", str(wav)]
    if args.delay:
        cmd += ["--delay", str(args.delay)]
    if args.log:
        cmd += ["--log", args.log]

    env = os.environ.copy()
    env.setdefault("STT_DIR", STT_DIR)
    env.setdefault("STT_MODEL", STT_MODEL)

    subprocess.run(cmd, env=env)


if __name__ == "__main__":
    main()

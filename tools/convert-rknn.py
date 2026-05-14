#!/usr/bin/env python3
"""Host-side RKNN conversion for Silero VAD and Whisper base.en.

Runs on x86_64 Linux only — rknn-toolkit2 is not published for aarch64 or
macOS. The resulting .rknn artifacts go into models/rknn/ and are tracked
via git-lfs.

Usage:
  python tools/convert-rknn.py --silero
  python tools/convert-rknn.py --whisper
  python tools/convert-rknn.py --all
  python tools/convert-rknn.py --all --quant int8 --calib path/to/wavs/

Default quantization is fp16 (no calibration data needed). INT8 needs a
directory of representative 16 kHz mono WAV files passed via --calib.
"""
from __future__ import annotations

import argparse
import os
import platform
import shutil
import subprocess
import sys
import tempfile
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
OUT_DIR = REPO_ROOT / "models" / "rknn"

# Pinned to a known-working revision in rknn_model_zoo so future repo churn
# doesn't break this script silently. Bump deliberately when Rockchip ships
# fixes worth picking up.
ZOO_REV = "v2.3.2"
ZOO_BASE = f"https://github.com/airockchip/rknn_model_zoo/raw/{ZOO_REV}/examples/whisper"
ZOO_MODEL = f"{ZOO_BASE}/model"
# Rockchip hosts the heavier Whisper ONNX artifacts off-GitHub; the model zoo
# repo has a download_model.sh that points here. The "_20s" variants match
# the 20-second chunk window the runtime in whisper_rknn.py expects.
ROCKCHIP_CDN_WHISPER = (
    "https://ftrg.zbox.filez.com/v2/delivery/data/"
    "95f00b0fc900458ba134f8b180b3f7a1/examples/whisper"
)


def die(msg: str, code: int = 1) -> None:
    print(f"convert-rknn: error: {msg}", file=sys.stderr)
    sys.exit(code)


def require_host() -> None:
    if sys.platform != "linux" or platform.machine() != "x86_64":
        die("rknn-toolkit2 only runs on x86_64 Linux "
            f"(this is {sys.platform}/{platform.machine()})")
    try:
        import rknn.api  # noqa: F401
    except ImportError:
        die("rknn-toolkit2 not installed; see "
            "https://github.com/airockchip/rknn-toolkit2/releases")


def download(url: str, dest: Path) -> None:
    if dest.exists():
        print(f"  cached: {dest}")
        return
    dest.parent.mkdir(parents=True, exist_ok=True)
    print(f"  fetching: {url}")
    urllib.request.urlretrieve(url, dest)


def build_calib_dataset(calib_dir: Path, work: Path) -> Path:
    """Build the dataset.txt that rknn-toolkit2 expects for INT8 quant.

    Each line is a path to an input .npy file. We convert each WAV in
    calib_dir to a numpy chunk matching the model's input shape.
    """
    import numpy as np
    import soundfile as sf

    wavs = sorted(p for p in calib_dir.iterdir() if p.suffix.lower() == ".wav")
    if not wavs:
        die(f"no .wav files in {calib_dir}")

    npy_dir = work / "calib_npy"
    npy_dir.mkdir(parents=True, exist_ok=True)
    dataset_txt = work / "dataset.txt"
    lines: list[str] = []
    for wav in wavs:
        audio, sr = sf.read(wav, dtype="float32")
        if sr != 16000 or audio.ndim != 1:
            print(f"  skip {wav.name}: need 16 kHz mono, got {sr} Hz, {audio.ndim}-ch")
            continue
        # Take the first 512-sample chunk (the Silero frame size).
        if audio.size < 512:
            continue
        chunk = audio[:512].reshape(1, 512).astype("float32")
        out = npy_dir / f"{wav.stem}.npy"
        np.save(out, chunk)
        lines.append(str(out))
    if not lines:
        die("no usable calibration samples")
    dataset_txt.write_text("\n".join(lines) + "\n")
    return dataset_txt


def convert_silero(quant: str, calib_dir: Path | None) -> None:
    from rknn.api import RKNN  # type: ignore

    print("=== Silero VAD ===")
    # Locate the bundled ONNX file without importing silero_vad — the package
    # imports torchaudio at module load, which drags CUDA libs into our
    # otherwise-CPU conversion container. We only want the .onnx artifact.
    import sysconfig
    site_dirs = [Path(sysconfig.get_paths()["purelib"]),
                 Path(sysconfig.get_paths()["platlib"])]
    onnx = None
    for site in site_dirs:
        candidates = list(site.rglob("silero_vad.onnx"))
        if candidates:
            onnx = candidates[0]
            break
    if onnx is None:
        die("silero_vad.onnx not found; pip install --no-deps silero-vad")
    print(f"  onnx: {onnx}")

    work = Path(tempfile.mkdtemp(prefix="silero-rknn-"))
    try:
        rknn = RKNN(verbose=False)
        rknn.config(target_platform="rk3588", quantized_dtype="w8a8")
        ret = rknn.load_onnx(model=str(onnx))
        if ret != 0:
            die(f"silero load_onnx failed: {ret}")

        do_quant = quant == "int8"
        dataset = None
        if do_quant:
            if calib_dir is None:
                die("--quant int8 requires --calib <dir of 16 kHz WAVs>")
            dataset = str(build_calib_dataset(calib_dir, work))
        ret = rknn.build(do_quantization=do_quant, dataset=dataset)
        if ret != 0:
            die(f"silero build failed: {ret}")

        out = OUT_DIR / "silero_vad.rknn"
        OUT_DIR.mkdir(parents=True, exist_ok=True)
        ret = rknn.export_rknn(str(out))
        if ret != 0:
            die(f"silero export_rknn failed: {ret}")
        rknn.release()
        print(f"  wrote: {out} ({out.stat().st_size:,} bytes)")
    finally:
        shutil.rmtree(work, ignore_errors=True)


def convert_whisper(quant: str, calib_dir: Path | None) -> None:
    from rknn.api import RKNN  # type: ignore

    print("=== Whisper base.en ===")
    work = Path(tempfile.mkdtemp(prefix="whisper-rknn-"))
    try:
        # Rockchip publishes the 20-second-chunk Whisper-base ONNX on their CDN.
        encoder_onnx = work / "whisper_encoder_base_20s.onnx"
        decoder_onnx = work / "whisper_decoder_base_20s.onnx"
        download(f"{ROCKCHIP_CDN_WHISPER}/whisper_encoder_base_20s.onnx", encoder_onnx)
        download(f"{ROCKCHIP_CDN_WHISPER}/whisper_decoder_base_20s.onnx", decoder_onnx)

        # The runtime needs the mel-filter coefficients and the BPE vocab
        # alongside the .rknn files. Drop them into models/rknn/ under
        # bush_stt's expected names.
        OUT_DIR.mkdir(parents=True, exist_ok=True)
        download(f"{ZOO_MODEL}/mel_80_filters.txt", OUT_DIR / "whisper_mel_80_filters.txt")
        download(f"{ZOO_MODEL}/vocab_en.txt", OUT_DIR / "whisper_vocab_en.txt")

        OUT_DIR.mkdir(parents=True, exist_ok=True)
        for stage, onnx, out_name in (
            ("encoder", encoder_onnx, "whisper_base_en_encoder.rknn"),
            ("decoder", decoder_onnx, "whisper_base_en_decoder.rknn"),
        ):
            print(f"  -- {stage} --")
            rknn = RKNN(verbose=False)
            # Whisper is sensitive to quant errors in attention; default fp16.
            rknn.config(target_platform="rk3588")
            ret = rknn.load_onnx(model=str(onnx))
            if ret != 0:
                die(f"whisper {stage} load_onnx failed: {ret}")
            # INT8 across the encoder needs mel-spec calib samples, not raw
            # PCM; out of scope for the first cut. Force fp16.
            ret = rknn.build(do_quantization=False)
            if ret != 0:
                die(f"whisper {stage} build failed: {ret}")
            out = OUT_DIR / out_name
            ret = rknn.export_rknn(str(out))
            if ret != 0:
                die(f"whisper {stage} export_rknn failed: {ret}")
            rknn.release()
            print(f"  wrote: {out} ({out.stat().st_size:,} bytes)")
    finally:
        shutil.rmtree(work, ignore_errors=True)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--silero", action="store_true")
    ap.add_argument("--whisper", action="store_true")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--quant", choices=["fp16", "int8"], default="fp16")
    ap.add_argument("--calib", type=Path, default=None,
                    help="Directory of 16 kHz mono WAV files for INT8 calibration")
    args = ap.parse_args()

    if not (args.silero or args.whisper or args.all):
        ap.print_help()
        return 2

    require_host()
    failures: list[str] = []
    if args.silero or args.all:
        try:
            convert_silero(args.quant, args.calib)
        except SystemExit as e:
            if args.silero and not args.all:
                raise
            failures.append(f"silero: exit {e.code}")
        except Exception as e:
            if args.silero and not args.all:
                raise
            failures.append(f"silero: {type(e).__name__}: {e}")
    if args.whisper or args.all:
        try:
            convert_whisper(args.quant, args.calib)
        except SystemExit as e:
            if args.whisper and not args.all:
                raise
            failures.append(f"whisper: exit {e.code}")
        except Exception as e:
            if args.whisper and not args.all:
                raise
            failures.append(f"whisper: {type(e).__name__}: {e}")
    if failures:
        print("\n--- partial failure ---", file=sys.stderr)
        for f in failures:
            print(f"  {f}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

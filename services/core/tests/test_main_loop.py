"""Unit tests for bush_tts main-loop helpers (engine factory + sox cmd builder).

These cover the new D8 dispatch layer. The full speak_worker is not exercised
here because it depends on subprocess + MQTT + a real engine.
"""
import importlib
import json
import sys
import types
from unittest.mock import MagicMock

import pytest


def _reload_bush_tts():
    """Reload bush_tts so module-level env-derived constants pick up monkeypatched env."""
    if "bush_tts" in sys.modules:
        del sys.modules["bush_tts"]
    return importlib.import_module("bush_tts")


# ── engine factory ──────────────────────────────────────────────────────────

def test_build_engine_returns_espeak_by_default(monkeypatch):
    monkeypatch.delenv("TTS_ENGINE", raising=False)
    bush_tts = _reload_bush_tts()
    engine = bush_tts._build_engine()
    from bush_tts.engines.espeak import EspeakEngine
    assert isinstance(engine, EspeakEngine)
    assert engine.name == "espeak"


def test_build_engine_returns_espeak_when_env_set(monkeypatch):
    monkeypatch.setenv("TTS_ENGINE", "espeak")
    bush_tts = _reload_bush_tts()
    engine = bush_tts._build_engine()
    from bush_tts.engines.espeak import EspeakEngine
    assert isinstance(engine, EspeakEngine)


def test_build_engine_case_insensitive(monkeypatch):
    monkeypatch.setenv("TTS_ENGINE", "ESPEAK")
    bush_tts = _reload_bush_tts()
    engine = bush_tts._build_engine()
    from bush_tts.engines.espeak import EspeakEngine
    assert isinstance(engine, EspeakEngine)


def test_build_engine_returns_piper(monkeypatch, tmp_path):
    """Stub PiperEngine so we don't need a real ONNX voice file on disk."""
    fake_voice = tmp_path / "fake.onnx"
    fake_voice.write_bytes(b"x")
    (tmp_path / "fake.onnx.json").write_text(json.dumps({"audio": {"sample_rate": 22050}}))

    monkeypatch.setenv("TTS_ENGINE", "piper")
    monkeypatch.setenv("PIPER_VOICE", str(fake_voice))
    monkeypatch.setenv("PIPER_BIN", "fake-piper")

    bush_tts = _reload_bush_tts()
    engine = bush_tts._build_engine()
    from bush_tts.engines.piper import PiperEngine
    assert isinstance(engine, PiperEngine)
    assert engine.name == "piper"


def test_build_engine_unknown_raises(monkeypatch):
    monkeypatch.setenv("TTS_ENGINE", "festival")
    bush_tts = _reload_bush_tts()
    with pytest.raises(RuntimeError, match="Unknown TTS_ENGINE"):
        bush_tts._build_engine()


# ── _sox_cmd ────────────────────────────────────────────────────────────────

def test_sox_cmd_default_device(monkeypatch):
    """When _tts_device is None, sox uses -d (default audio device)."""
    monkeypatch.delenv("TTS_ENGINE", raising=False)
    bush_tts = _reload_bush_tts()
    bush_tts._tts_device = None
    bush_tts._tts_clarity = 0
    cmd = bush_tts._sox_cmd(22050)
    assert cmd[0] == "sox"
    assert "-d" in cmd
    # raw-PCM input shape:
    assert "-t" in cmd and cmd[cmd.index("-t") + 1] == "raw"
    assert "-r" in cmd and cmd[cmd.index("-r") + 1] == "22050"
    assert "-e" in cmd and cmd[cmd.index("-e") + 1] == "signed"
    assert "-b" in cmd and cmd[cmd.index("-b") + 1] == "16"
    assert "-c" in cmd and cmd[cmd.index("-c") + 1] == "1"
    # Stdin marker:
    assert "-" in cmd


def test_sox_cmd_alsa_device(monkeypatch):
    """When _tts_device is 'hw:Card', sox uses -t alsa hw:Card."""
    monkeypatch.delenv("TTS_ENGINE", raising=False)
    bush_tts = _reload_bush_tts()
    bush_tts._tts_device = "hw:Card"
    bush_tts._tts_clarity = 0
    cmd = bush_tts._sox_cmd(22050)
    # The output -t alsa hw:Card sequence must appear consecutively
    # (input is "-t raw"; output is "-t alsa <dev>").
    raw_idx = cmd.index("raw")
    alsa_idx = cmd.index("alsa")
    assert alsa_idx > raw_idx
    assert cmd[alsa_idx - 1] == "-t"
    assert cmd[alsa_idx + 1] == "hw:Card"


def test_sox_cmd_sample_rate_propagates(monkeypatch):
    """Different engines yield different sample rates; sox must reflect that."""
    monkeypatch.delenv("TTS_ENGINE", raising=False)
    bush_tts = _reload_bush_tts()
    bush_tts._tts_device = None
    bush_tts._tts_clarity = 0
    cmd_22k = bush_tts._sox_cmd(22050)
    cmd_24k = bush_tts._sox_cmd(24000)
    cmd_16k = bush_tts._sox_cmd(16000)
    assert cmd_22k[cmd_22k.index("-r") + 1] == "22050"
    assert cmd_24k[cmd_24k.index("-r") + 1] == "24000"
    assert cmd_16k[cmd_16k.index("-r") + 1] == "16000"


def test_sox_cmd_appends_clarity_effects(monkeypatch):
    """The output of build_sox_effects(clarity) must be appended at the end."""
    monkeypatch.delenv("TTS_ENGINE", raising=False)
    bush_tts = _reload_bush_tts()
    from bushutil import build_sox_effects
    bush_tts._tts_device = None
    bush_tts._tts_clarity = 50
    cmd = bush_tts._sox_cmd(22050)
    expected_effects = build_sox_effects(50)
    # The last len(expected_effects) tokens of cmd should equal expected_effects
    assert cmd[-len(expected_effects):] == expected_effects
    # Effects appear after the output device args
    assert cmd.index("gain") > cmd.index("-d")

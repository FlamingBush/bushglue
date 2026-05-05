"""Unit tests for PiperEngine."""
import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest


@pytest.fixture
def voice_config(tmp_path):
    """Create a fake voice .onnx + .onnx.json pair."""
    voice = tmp_path / "fake-voice.onnx"
    voice.write_bytes(b"fake-onnx")
    config = tmp_path / "fake-voice.onnx.json"
    config.write_text(json.dumps({
        "audio": {"sample_rate": 22050},
        "language": {"code": "en-GB"},
    }))
    return str(voice)


def test_constructor_reads_sample_rate(voice_config):
    from bush_tts.engines.piper import PiperEngine
    e = PiperEngine(voice_path=voice_config, binary_path="fake-piper")
    assert e.name == "piper"
    assert e._sample_rate == 22050


def test_constructor_uses_24k_voice(tmp_path):
    voice = tmp_path / "v.onnx"
    voice.write_bytes(b"x")
    (tmp_path / "v.onnx.json").write_text(json.dumps({"audio": {"sample_rate": 24000}}))
    from bush_tts.engines.piper import PiperEngine
    e = PiperEngine(voice_path=str(voice), binary_path="fake-piper")
    assert e._sample_rate == 24000


def test_missing_voice_raises():
    from bush_tts.engines.piper import PiperEngine
    with pytest.raises(RuntimeError, match="PIPER_VOICE"):
        PiperEngine(binary_path="x")


def test_missing_voice_config_raises(tmp_path):
    voice = tmp_path / "v.onnx"
    voice.write_bytes(b"x")
    # no .json
    from bush_tts.engines.piper import PiperEngine
    with pytest.raises(RuntimeError, match="config not found"):
        PiperEngine(voice_path=str(voice), binary_path="x")


def test_empty_text_returns_empty_audio(voice_config):
    from bush_tts.engines.piper import PiperEngine
    e = PiperEngine(voice_path=voice_config, binary_path="fake-piper")
    result = e.synthesize("")
    assert result["audio_pcm"] == b""
    assert result["sample_rate"] == 22050


def test_synthesize_invokes_piper(voice_config, monkeypatch):
    from bush_tts.engines.piper import PiperEngine
    e = PiperEngine(voice_path=voice_config, binary_path="fake-piper")
    captured = {}
    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        captured["input"] = kwargs.get("input")
        return MagicMock(returncode=0, stdout=b"\x10\x00" * 1000, stderr=b"")
    monkeypatch.setattr("subprocess.run", fake_run)
    result = e.synthesize("hello world")
    cmd = captured["cmd"]
    assert cmd[0] == "fake-piper"
    assert "--model" in cmd and cmd[cmd.index("--model") + 1] == voice_config
    assert "--output_raw" in cmd
    assert captured["input"] == b"hello world"
    assert result["sample_rate"] == 22050
    assert len(result["audio_pcm"]) == 2000


def test_non_zero_exit_raises(voice_config, monkeypatch):
    from bush_tts.engines.piper import PiperEngine
    e = PiperEngine(voice_path=voice_config, binary_path="x")
    monkeypatch.setattr("subprocess.run", lambda *a, **kw: MagicMock(returncode=2, stdout=b"", stderr=b"err"))
    with pytest.raises(RuntimeError, match="piper failed"):
        e.synthesize("hi")


def test_close_makes_synthesize_raise(voice_config):
    from bush_tts.engines.piper import PiperEngine
    e = PiperEngine(voice_path=voice_config, binary_path="x")
    e.close()
    with pytest.raises(RuntimeError, match="closed"):
        e.synthesize("hi")


def test_satisfies_protocol(voice_config):
    from bush_tts.engines.base import TTSEngine
    from bush_tts.engines.piper import PiperEngine
    assert isinstance(PiperEngine(voice_path=voice_config, binary_path="x"), TTSEngine)

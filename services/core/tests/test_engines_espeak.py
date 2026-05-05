"""Unit tests for EspeakEngine."""
import io
import struct
import wave
from unittest.mock import MagicMock

import pytest


def make_wav_bytes(sample_rate: int = 22050, audio_int16: bytes = b"\x10\x00\x00\x10") -> bytes:
    """Build a minimal WAV byte string."""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(audio_int16)
    return buf.getvalue()


@pytest.fixture
def engine():
    from bush_tts.engines.espeak import EspeakEngine
    return EspeakEngine()


def test_engine_constructs(engine):
    assert engine.name == "espeak"


def test_empty_text_returns_empty_audio(engine):
    result = engine.synthesize("")
    assert result["audio_pcm"] == b""
    assert result["sample_rate"] == 22050
    assert "ts" in result


def test_strips_whitespace(engine, monkeypatch):
    captured = {}
    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        return MagicMock(returncode=0, stdout=make_wav_bytes(), stderr=b"")
    monkeypatch.setattr("subprocess.run", fake_run)
    engine.synthesize("   hello   ")
    assert captured["cmd"][-1] == "hello"


def test_synthesize_invokes_espeak(engine, monkeypatch):
    captured = {}
    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        return MagicMock(returncode=0, stdout=make_wav_bytes(audio_int16=b"\x10\x00" * 100), stderr=b"")
    monkeypatch.setattr("subprocess.run", fake_run)
    result = engine.synthesize("hello world")
    cmd = captured["cmd"]
    assert cmd[0] == "espeak-ng"
    assert "-v" in cmd and cmd[cmd.index("-v") + 1] == "en-gb"
    assert "-s" in cmd and cmd[cmd.index("-s") + 1] == "95"
    assert "-p" in cmd and cmd[cmd.index("-p") + 1] == "1"
    assert "-a" in cmd and cmd[cmd.index("-a") + 1] == "200"
    assert "--stdout" in cmd
    assert cmd[-1] == "hello world"
    assert result["sample_rate"] == 22050
    assert len(result["audio_pcm"]) == 200  # 100 samples * 2 bytes


def test_non_zero_exit_raises(engine, monkeypatch):
    monkeypatch.setattr("subprocess.run", lambda *a, **kw: MagicMock(returncode=1, stdout=b"", stderr=b"oops"))
    with pytest.raises(RuntimeError, match="espeak-ng failed"):
        engine.synthesize("hi")


def test_close_makes_synthesize_raise(engine):
    engine.close()
    with pytest.raises(RuntimeError, match="closed"):
        engine.synthesize("hi")


def test_close_is_idempotent(engine):
    engine.close()
    engine.close()


def test_satisfies_protocol(engine):
    from bush_tts.engines.base import TTSEngine
    assert isinstance(engine, TTSEngine)


def test_env_vars(monkeypatch):
    monkeypatch.setenv("ESPEAK_VOICE", "en-us")
    monkeypatch.setenv("ESPEAK_SPEED", "120")
    from bush_tts.engines.espeak import EspeakEngine
    e = EspeakEngine()
    assert e._voice == "en-us"
    assert e._speed == 120

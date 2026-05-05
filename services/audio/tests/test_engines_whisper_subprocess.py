"""Unit tests for WhisperSubprocessEngine."""
import struct
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


def make_engine(**kwargs):
    from bush_stt.engines.whisper_subprocess import WhisperSubprocessEngine
    return WhisperSubprocessEngine(
        binary_path=kwargs.pop("binary_path", "fake-whisper"),
        model_path=kwargs.pop("model_path", "/fake/model.bin"),
        **kwargs,
    )


def test_engine_constructs_with_defaults(monkeypatch):
    monkeypatch.setenv("WHISPER_BIN", "envbin")
    monkeypatch.setenv("WHISPER_MODEL", "/env/model.bin")
    monkeypatch.setenv("WHISPER_THREADS", "8")
    monkeypatch.setenv("WHISPER_LANGUAGE", "es")
    from bush_stt.engines.whisper_subprocess import WhisperSubprocessEngine
    e = WhisperSubprocessEngine()
    assert e.name == "whisper-subprocess"
    assert e.sample_rate == 16000
    assert e._binary == "envbin"
    assert e._model == "/env/model.bin"
    assert e._threads == 8
    assert e._language == "es"


def test_empty_audio_returns_empty_text():
    e = make_engine()
    result = e.transcribe(b"")
    assert result["text"] == ""
    assert result["confidence"] == 0.0


def test_transcribe_runs_binary_and_reads_output(tmp_path, monkeypatch):
    e = make_engine()

    def fake_run(cmd, *args, **kwargs):
        # The cmd has -of <base>; write the expected .txt
        base = cmd[cmd.index("-of") + 1]
        Path(base + ".txt").write_text("hello world\n")
        return MagicMock(returncode=0, stderr="", stdout="")

    monkeypatch.setattr("subprocess.run", fake_run)
    audio = b"\x00\x01" * 16000  # 1 second
    result = e.transcribe(audio)
    assert result["text"] == "hello world"
    assert result["confidence"] == 1.0


def test_transcribe_non_zero_exit_returns_empty(monkeypatch):
    e = make_engine()
    monkeypatch.setattr("subprocess.run", lambda *a, **kw: MagicMock(returncode=1, stderr="boom", stdout=""))
    result = e.transcribe(b"\x00\x01" * 16000)
    assert result["text"] == ""
    assert result["confidence"] == 0.0


def test_transcribe_timeout_returns_empty(monkeypatch):
    e = make_engine()
    def fake_run(*a, **kw):
        raise subprocess.TimeoutExpired(cmd="whisper-cli", timeout=30)
    monkeypatch.setattr("subprocess.run", fake_run)
    result = e.transcribe(b"\x00\x01" * 16000)
    assert result["text"] == ""


def test_transcribe_missing_output_file_returns_empty(monkeypatch):
    e = make_engine()
    # Run "succeeds" but never writes the .txt
    monkeypatch.setattr("subprocess.run", lambda *a, **kw: MagicMock(returncode=0, stderr="", stdout=""))
    result = e.transcribe(b"\x00\x01" * 16000)
    assert result["text"] == ""


def test_close_makes_transcribe_raise():
    e = make_engine()
    e.close()
    with pytest.raises(RuntimeError, match="closed"):
        e.transcribe(b"\x00\x01" * 16000)


def test_close_is_idempotent():
    e = make_engine()
    e.close()
    e.close()


def test_wav_file_format(tmp_path, monkeypatch):
    """Verify the WAV file the engine writes is well-formed RIFF."""
    e = make_engine()
    captured: dict = {}

    def fake_run(cmd, *args, **kwargs):
        wav_path = cmd[cmd.index("-f") + 1]
        captured["wav_bytes"] = Path(wav_path).read_bytes()
        base = cmd[cmd.index("-of") + 1]
        Path(base + ".txt").write_text("ok")
        return MagicMock(returncode=0, stderr="", stdout="")

    monkeypatch.setattr("subprocess.run", fake_run)
    audio = b"\x00\x01" * 8000  # 0.5 second
    e.transcribe(audio)
    wav = captured["wav_bytes"]
    assert wav[:4] == b"RIFF"
    assert wav[8:12] == b"WAVE"
    assert wav[12:16] == b"fmt "
    sample_rate, = struct.unpack("<I", wav[24:28])
    assert sample_rate == 16000
    n_channels, = struct.unpack("<H", wav[22:24])
    assert n_channels == 1


def test_satisfies_protocol():
    from bush_stt.engines.base import STTEngine
    e = make_engine()
    assert isinstance(e, STTEngine)

"""Unit tests for WhisperBindingsEngine."""
import sys
from unittest.mock import MagicMock

import pytest


@pytest.fixture(autouse=True)
def stub_pywhispercpp(monkeypatch):
    """Stub pywhispercpp.model at import time."""
    fake_module = MagicMock()
    fake_model_module = MagicMock()
    fake_model_class = MagicMock()
    fake_model_module.Model = fake_model_class
    fake_module.model = fake_model_module
    monkeypatch.setitem(sys.modules, "pywhispercpp", fake_module)
    monkeypatch.setitem(sys.modules, "pywhispercpp.model", fake_model_module)
    yield fake_model_module


@pytest.fixture
def engine(stub_pywhispercpp):
    from bush_stt.engines.whisper_bindings import WhisperBindingsEngine
    return WhisperBindingsEngine(model_path_or_name="base.en")


def test_engine_constructs_with_defaults(stub_pywhispercpp):
    from bush_stt.engines.whisper_bindings import WhisperBindingsEngine
    e = WhisperBindingsEngine(model_path_or_name="base.en")
    assert e.name == "whisper-bindings"
    assert e.sample_rate == 16000
    stub_pywhispercpp.Model.assert_called_once()
    args, kwargs = stub_pywhispercpp.Model.call_args
    assert args[0] == "base.en"
    assert kwargs.get("n_threads") == 4
    assert kwargs.get("language") == "en"


def test_engine_reads_env_vars(monkeypatch, stub_pywhispercpp):
    monkeypatch.setenv("WHISPER_MODEL", "small.en")
    monkeypatch.setenv("WHISPER_THREADS", "8")
    monkeypatch.setenv("WHISPER_LANGUAGE", "es")
    from bush_stt.engines.whisper_bindings import WhisperBindingsEngine
    e = WhisperBindingsEngine()
    args, kwargs = stub_pywhispercpp.Model.call_args
    assert args[0] == "small.en"
    assert kwargs["n_threads"] == 8
    assert kwargs["language"] == "es"


def test_empty_audio_returns_empty_text(engine):
    result = engine.transcribe(b"")
    assert result["text"] == ""
    assert result["confidence"] == 0.0
    assert "ts" in result


def test_transcribe_concatenates_segment_text(engine, stub_pywhispercpp):
    seg1 = MagicMock(text=" hello ")
    seg2 = MagicMock(text=" world ")
    engine._model.transcribe.return_value = [seg1, seg2]
    result = engine.transcribe(b"\x00\x01" * 16000)  # 1 second of fake audio
    assert result["text"] == "hello world"
    assert result["confidence"] == 1.0


def test_transcribe_handles_dict_segments(engine, stub_pywhispercpp):
    engine._model.transcribe.return_value = [{"text": "hello"}, {"text": "world"}]
    result = engine.transcribe(b"\x00\x01" * 16000)
    assert result["text"] == "hello world"


def test_transcribe_empty_segments_returns_empty(engine, stub_pywhispercpp):
    engine._model.transcribe.return_value = []
    result = engine.transcribe(b"\x00\x01" * 16000)
    assert result["text"] == ""
    assert result["confidence"] == 0.0


def test_close_makes_transcribe_raise(engine):
    engine.close()
    with pytest.raises(RuntimeError, match="closed"):
        engine.transcribe(b"\x00\x01" * 16000)


def test_close_is_idempotent(engine):
    engine.close()
    engine.close()


def test_satisfies_protocol(engine):
    from bush_stt.engines.base import STTEngine
    assert isinstance(engine, STTEngine)

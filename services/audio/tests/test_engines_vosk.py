"""Unit tests for VoskEngine adapter."""
import json
from unittest.mock import MagicMock, patch

import pytest


# Stub vosk module BEFORE importing the engine
@pytest.fixture(autouse=True)
def stub_vosk(monkeypatch):
    fake_vosk = MagicMock()
    fake_vosk.Model = MagicMock()
    fake_vosk.KaldiRecognizer = MagicMock()
    monkeypatch.setitem(__import__("sys").modules, "vosk", fake_vosk)
    yield fake_vosk


@pytest.fixture
def engine(stub_vosk):
    from bush_stt.engines.vosk import VoskEngine
    return VoskEngine(model_path="/fake/path")


def test_engine_constructs(stub_vosk):
    from bush_stt.engines.vosk import VoskEngine
    e = VoskEngine(model_path="/fake/path")
    assert e.name == "vosk"
    assert e.sample_rate == 16000
    stub_vosk.Model.assert_called_once_with("/fake/path")


def test_empty_audio_returns_empty_text(engine):
    result = engine.transcribe(b"")
    assert result["text"] == ""
    assert result["confidence"] == 0.0
    assert "ts" in result


def test_transcribe_returns_parsed_text(engine, stub_vosk):
    rec = MagicMock()
    rec.FinalResult.return_value = json.dumps({"text": "hello world"})
    stub_vosk.KaldiRecognizer.return_value = rec
    result = engine.transcribe(b"x" * 32000)
    rec.AcceptWaveform.assert_called_once_with(b"x" * 32000)
    rec.FinalResult.assert_called_once()
    assert result["text"] == "hello world"
    assert result["confidence"] == 1.0  # binary fallback when no word-level conf
    assert "ts" in result


def test_transcribe_uses_word_level_confidence(engine, stub_vosk):
    rec = MagicMock()
    rec.FinalResult.return_value = json.dumps({
        "text": "hello world",
        "result": [
            {"conf": 0.9, "word": "hello"},
            {"conf": 0.7, "word": "world"},
        ],
    })
    stub_vosk.KaldiRecognizer.return_value = rec
    result = engine.transcribe(b"x" * 32000)
    assert result["text"] == "hello world"
    assert abs(result["confidence"] - 0.8) < 1e-6  # mean of 0.9 and 0.7


def test_transcribe_strips_whitespace(engine, stub_vosk):
    rec = MagicMock()
    rec.FinalResult.return_value = json.dumps({"text": "  padded  "})
    stub_vosk.KaldiRecognizer.return_value = rec
    result = engine.transcribe(b"x" * 32000)
    assert result["text"] == "padded"


def test_close_makes_transcribe_raise(engine):
    engine.close()
    with pytest.raises(RuntimeError, match="closed"):
        engine.transcribe(b"x" * 100)


def test_close_is_idempotent(engine):
    engine.close()
    engine.close()  # should not raise


def test_satisfies_protocol(engine):
    from bush_stt.engines.base import STTEngine
    assert isinstance(engine, STTEngine)

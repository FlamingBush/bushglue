"""Unit tests for stt postprocess (LLM correction)."""
import json
import urllib.error
from unittest.mock import MagicMock

import pytest


def test_disabled_returns_input_unchanged(monkeypatch):
    monkeypatch.delenv("STT_LLM_CORRECT", raising=False)
    from bush_stt.postprocess import correct_transcript
    assert correct_transcript("burning bus") == "burning bus"


def test_explicit_disable_wins_over_env(monkeypatch):
    monkeypatch.setenv("STT_LLM_CORRECT", "1")
    from bush_stt.postprocess import correct_transcript
    assert correct_transcript("burning bus", enabled=False) == "burning bus"


def test_empty_input_short_circuits():
    from bush_stt.postprocess import correct_transcript
    assert correct_transcript("", enabled=True) == ""


def test_correction_returned_when_ollama_responds(monkeypatch):
    fake_response = MagicMock()
    fake_response.read.return_value = json.dumps({"response": "burning bush"}).encode()
    fake_response.__enter__ = lambda s: s
    fake_response.__exit__ = lambda *a: None
    monkeypatch.setattr("urllib.request.urlopen", lambda *a, **kw: fake_response)
    from bush_stt.postprocess import correct_transcript
    assert correct_transcript("burning bus", enabled=True) == "burning bush"


def test_timeout_falls_back_to_raw(monkeypatch):
    def raise_timeout(*a, **kw):
        raise urllib.error.URLError("timeout")
    monkeypatch.setattr("urllib.request.urlopen", raise_timeout)
    from bush_stt.postprocess import correct_transcript
    assert correct_transcript("burning bus", enabled=True) == "burning bus"


def test_empty_response_falls_back(monkeypatch):
    fake_response = MagicMock()
    fake_response.read.return_value = json.dumps({"response": ""}).encode()
    fake_response.__enter__ = lambda s: s
    fake_response.__exit__ = lambda *a: None
    monkeypatch.setattr("urllib.request.urlopen", lambda *a, **kw: fake_response)
    from bush_stt.postprocess import correct_transcript
    assert correct_transcript("burning bus", enabled=True) == "burning bus"


def test_runaway_response_falls_back(monkeypatch):
    """If the LLM returns something 3x longer than input + 30, treat as suspicious."""
    runaway = "x" * 200
    fake_response = MagicMock()
    fake_response.read.return_value = json.dumps({"response": runaway}).encode()
    fake_response.__enter__ = lambda s: s
    fake_response.__exit__ = lambda *a: None
    monkeypatch.setattr("urllib.request.urlopen", lambda *a, **kw: fake_response)
    from bush_stt.postprocess import correct_transcript
    assert correct_transcript("burning bus", enabled=True) == "burning bus"

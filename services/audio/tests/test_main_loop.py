"""Unit tests for bush_stt main-loop helpers.

Focus on the unit-testable parts: env-var parsing, capture-rate selection,
chunk sizing, the engine factory and the pipeline factory. The capture loop
itself (subprocess + threads + MQTT) is out of scope here; integration is
covered by the bench harness and on-target validation.
"""
from __future__ import annotations

import importlib
import sys
from unittest.mock import MagicMock

import pytest


@pytest.fixture(autouse=True)
def stub_modules(monkeypatch):
    """Stub heavy deps at sys.modules so importing bush_stt is cheap."""
    fakes: dict[str, MagicMock] = {}
    for mod_name, members in [
        ("vosk", ["Model", "KaldiRecognizer"]),
        ("silero_vad", ["load_silero_vad"]),
        ("pyrnnoise", ["RNNoise"]),
        ("soxr", ["ResampleStream", "resample"]),
        # whisper bindings: lazy-imported inside engines/whisper_bindings.py
        # via `from pywhispercpp.model import Model`. Stub the parent package
        # too so the dotted submodule import resolves.
        ("pywhispercpp", []),
        ("pywhispercpp.model", ["Model"]),
    ]:
        m = MagicMock()
        for name in members:
            setattr(m, name, MagicMock())
        monkeypatch.setitem(sys.modules, mod_name, m)
        fakes[mod_name] = m
    return fakes


def _reload_bush_stt():
    """Drop the cached bush_stt and re-import so env-var changes take effect."""
    for mod in list(sys.modules):
        if mod == "bush_stt" or mod.startswith("bush_stt."):
            del sys.modules[mod]
    import bush_stt  # noqa: F401
    return sys.modules["bush_stt"]


def test_default_flags_use_legacy_pipeline(monkeypatch):
    monkeypatch.delenv("STT_USE_VAD", raising=False)
    monkeypatch.delenv("STT_USE_RNNOISE", raising=False)
    monkeypatch.delenv("STT_ENGINE", raising=False)
    bush_stt = _reload_bush_stt()
    assert bush_stt.STT_USE_VAD is False
    assert bush_stt.STT_USE_RNNOISE is False
    assert bush_stt.STT_ENGINE_NAME == "vosk"
    assert bush_stt.CAPTURE_SAMPLE_RATE == 16000
    assert bush_stt.RECOGNIZER_SAMPLE_RATE == 16000
    # Legacy path's existing constant must remain available.
    assert bush_stt.SAMPLE_RATE == 16000


def test_falsy_strings_keep_flags_off(monkeypatch):
    """Common falsy spellings should leave flags off (matches contract)."""
    for falsy in ("0", "false", "False", ""):
        monkeypatch.setenv("STT_USE_VAD", falsy)
        monkeypatch.setenv("STT_USE_RNNOISE", falsy)
        bush_stt = _reload_bush_stt()
        assert bush_stt.STT_USE_VAD is False, f"VAD should be off for {falsy!r}"
        assert bush_stt.STT_USE_RNNOISE is False, f"RNNoise should be off for {falsy!r}"
        assert bush_stt.CAPTURE_SAMPLE_RATE == 16000


def test_vad_only_keeps_16k_capture(monkeypatch):
    monkeypatch.setenv("STT_USE_VAD", "1")
    monkeypatch.delenv("STT_USE_RNNOISE", raising=False)
    bush_stt = _reload_bush_stt()
    assert bush_stt.STT_USE_VAD is True
    assert bush_stt.STT_USE_RNNOISE is False
    assert bush_stt.CAPTURE_SAMPLE_RATE == 16000


def test_rnnoise_flips_capture_to_48k(monkeypatch):
    monkeypatch.setenv("STT_USE_VAD", "1")
    monkeypatch.setenv("STT_USE_RNNOISE", "1")
    bush_stt = _reload_bush_stt()
    assert bush_stt.CAPTURE_SAMPLE_RATE == 48000


def test_rnnoise_without_vad_does_not_flip_capture_rate(monkeypatch):
    """RNNoise without VAD is meaningless — leave capture at 16k for legacy path."""
    monkeypatch.delenv("STT_USE_VAD", raising=False)
    monkeypatch.setenv("STT_USE_RNNOISE", "1")
    bush_stt = _reload_bush_stt()
    assert bush_stt.STT_USE_VAD is False
    assert bush_stt.STT_USE_RNNOISE is True  # raw flag is set; gets ignored downstream
    assert bush_stt.CAPTURE_SAMPLE_RATE == 16000


def test_chunk_size_matches_capture_rate(monkeypatch):
    """0.5 s of int16 LE audio at the active capture rate."""
    monkeypatch.setenv("STT_USE_VAD", "1")
    monkeypatch.setenv("STT_USE_RNNOISE", "1")
    bush_stt = _reload_bush_stt()
    assert bush_stt.CHUNK == int(48000 * 0.5) * 2  # 48000 bytes
    # And legacy default
    monkeypatch.delenv("STT_USE_VAD", raising=False)
    monkeypatch.delenv("STT_USE_RNNOISE", raising=False)
    bush_stt = _reload_bush_stt()
    assert bush_stt.CHUNK == int(16000 * 0.5) * 2  # 16000 bytes


def test_engine_name_lowercased(monkeypatch):
    monkeypatch.setenv("STT_USE_VAD", "1")
    monkeypatch.setenv("STT_ENGINE", "WHISPER-BINDINGS")
    bush_stt = _reload_bush_stt()
    assert bush_stt.STT_ENGINE_NAME == "whisper-bindings"


def test_stt_min_confidence_default(monkeypatch):
    """STT_MIN_CONFIDENCE defaults to 0.6 (matches prior STT-accuracy work)."""
    monkeypatch.delenv("STT_MIN_CONFIDENCE", raising=False)
    bush_stt = _reload_bush_stt()
    assert bush_stt.STT_MIN_CONFIDENCE == 0.6


def test_stt_min_confidence_env_override(monkeypatch):
    """STT_MIN_CONFIDENCE env var overrides the default."""
    monkeypatch.setenv("STT_MIN_CONFIDENCE", "0.85")
    bush_stt = _reload_bush_stt()
    assert bush_stt.STT_MIN_CONFIDENCE == 0.85
    monkeypatch.setenv("STT_MIN_CONFIDENCE", "0.0")
    bush_stt = _reload_bush_stt()
    assert bush_stt.STT_MIN_CONFIDENCE == 0.0


def test_build_engine_routes_to_vosk(monkeypatch):
    monkeypatch.setenv("STT_USE_VAD", "1")
    monkeypatch.setenv("STT_ENGINE", "vosk")
    bush_stt = _reload_bush_stt()
    assert callable(bush_stt._build_engine)
    engine = bush_stt._build_engine()
    # VoskEngine adapter is constructed; sample rate is the recognizer's.
    assert engine.name == "vosk"
    assert engine.sample_rate == 16000


def test_build_engine_routes_to_whisper_bindings(monkeypatch, stub_modules):
    monkeypatch.setenv("STT_USE_VAD", "1")
    monkeypatch.setenv("STT_ENGINE", "whisper-bindings")
    bush_stt = _reload_bush_stt()
    engine = bush_stt._build_engine()
    assert engine.name == "whisper-bindings"


def test_build_engine_routes_to_whisper_subprocess(monkeypatch):
    monkeypatch.setenv("STT_USE_VAD", "1")
    monkeypatch.setenv("STT_ENGINE", "whisper-subprocess")
    bush_stt = _reload_bush_stt()
    engine = bush_stt._build_engine()
    assert engine.name == "whisper-subprocess"


def test_build_engine_unknown_raises(monkeypatch):
    monkeypatch.setenv("STT_USE_VAD", "1")
    monkeypatch.setenv("STT_ENGINE", "bogus")
    bush_stt = _reload_bush_stt()
    with pytest.raises(RuntimeError, match="Unknown STT_ENGINE"):
        bush_stt._build_engine()


def test_build_pipeline_returns_no_denoise_when_rnnoise_off(monkeypatch):
    monkeypatch.setenv("STT_USE_VAD", "1")
    monkeypatch.delenv("STT_USE_RNNOISE", raising=False)
    bush_stt = _reload_bush_stt()
    vad, denoise, resampler = bush_stt._build_pipeline()
    assert vad is not None
    assert denoise is None
    assert resampler is None


def test_build_pipeline_includes_denoise_and_resampler_when_on(monkeypatch):
    monkeypatch.setenv("STT_USE_VAD", "1")
    monkeypatch.setenv("STT_USE_RNNOISE", "1")
    bush_stt = _reload_bush_stt()
    vad, denoise, resampler = bush_stt._build_pipeline()
    assert vad is not None
    assert denoise is not None
    assert resampler is not None  # soxr.ResampleStream MagicMock


def test_legacy_path_still_imports_speechtotext(monkeypatch):
    """Ensure the legacy SpeechToText path's imports haven't been broken."""
    monkeypatch.delenv("STT_USE_VAD", raising=False)
    bush_stt = _reload_bush_stt()  # noqa: F841
    from bush_stt.transcriber import SpeechToText
    assert SpeechToText is not None


def test_mqtt_topic_constants_unchanged(monkeypatch):
    """Topic constants are part of the contract — must stay in place even
    though the new path doesn't publish to TOPIC_PARTIAL."""
    bush_stt = _reload_bush_stt()
    assert bush_stt.TOPIC_TRANSCRIPT == "bush/pipeline/stt/transcript"
    assert bush_stt.TOPIC_PARTIAL == "bush/pipeline/stt/partial"
    assert bush_stt.TOPIC_TTS_SPEAKING == "bush/pipeline/tts/speaking"
    assert bush_stt.TOPIC_TTS_DONE == "bush/pipeline/tts/done"
    assert bush_stt.TOPIC_FORCE_FINALIZE == "bush/pipeline/stt/force-finalize"
    assert bush_stt.TOPIC_SET_DEVICE == "bush/audio/stt/set-device"
    assert bush_stt.TOPIC_DEVICE_STATUS == "bush/audio/stt/device"
    assert bush_stt.TOPIC_PIPELINE_PING == "bush/pipeline/ping"
    assert bush_stt.TOPIC_PIPELINE_PONG == "bush/pipeline/pong"
    assert bush_stt.TOPIC_TTS_DEVICE == "bush/audio/tts/device"


def test_fallback_phrases_intact(monkeypatch):
    """_next_fallback() should still cycle through FALLBACK_PHRASES."""
    bush_stt = _reload_bush_stt()
    assert len(bush_stt.FALLBACK_PHRASES) >= 10
    # Drain a full cycle and make sure each phrase is from the list.
    seen = set()
    for _ in range(len(bush_stt.FALLBACK_PHRASES)):
        seen.add(bush_stt._next_fallback())
    assert seen == set(bush_stt.FALLBACK_PHRASES)

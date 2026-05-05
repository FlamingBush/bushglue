"""Unit tests for VadEndpointer."""
import pytest

from bush_stt.vad import (
    VadEndpointer,
    SAMPLE_RATE,
    FRAME_SAMPLES,
    FRAME_BYTES,
    FRAME_MS,
)


class ScriptedVadModel:
    """Test stub: returns probabilities from a pre-set list, one per call."""
    def __init__(self, probs):
        self._probs = list(probs)
        self._calls = 0

    def __call__(self, audio, sr):
        if self._calls < len(self._probs):
            p = self._probs[self._calls]
        else:
            p = self._probs[-1] if self._probs else 0.0
        self._calls += 1
        return p


def make_endpointer(probs, **overrides):
    return VadEndpointer(model_loader=lambda: ScriptedVadModel(probs), **overrides)


def silence_frames(n_frames: int) -> bytes:
    return b"\x00" * (FRAME_BYTES * n_frames)


def voiced_frames(n_frames: int) -> bytes:
    # Non-zero PCM so we can distinguish from silence in assertions
    return b"\x10\x00" * (FRAME_SAMPLES * n_frames)


def ms_to_frames(ms: int) -> int:
    return max(1, ms // FRAME_MS)


def test_voice_then_silence_emits_one_utterance():
    voice_n = ms_to_frames(400)   # > min_utterance_ms (250)
    silence_n = ms_to_frames(800) # > min_silence_ms (600)
    probs = [0.9] * voice_n + [0.1] * silence_n
    ep = make_endpointer(probs)
    emitted = ep.feed(voiced_frames(voice_n) + silence_frames(silence_n))
    assert len(emitted) == 1, f"expected 1 utterance, got {len(emitted)}"
    duration_ms = (len(emitted[0]) * 1000) // (SAMPLE_RATE * 2)
    assert duration_ms >= 400, f"utterance shorter than expected: {duration_ms}ms"


def test_short_voice_filtered_as_noise():
    # 100ms voice (below 250ms min) followed by long silence
    voice_n = ms_to_frames(100)
    silence_n = ms_to_frames(800)
    probs = [0.9] * voice_n + [0.1] * silence_n
    ep = make_endpointer(probs)
    emitted = ep.feed(voiced_frames(voice_n) + silence_frames(silence_n))
    assert emitted == [], f"expected no utterance for short voice, got {len(emitted)}"


def test_max_utterance_force_cut():
    # 16000ms continuous voice — should force-cut at 15000ms
    voice_n = ms_to_frames(16000)
    probs = [0.9] * voice_n
    ep = make_endpointer(probs)
    emitted = ep.feed(voiced_frames(voice_n))
    assert len(emitted) >= 1, "expected at least one force-cut emission"
    duration_ms = (len(emitted[0]) * 1000) // (SAMPLE_RATE * 2)
    assert 14000 <= duration_ms <= 15500, f"force-cut size unexpected: {duration_ms}ms"


def test_drop_in_flight_discards_buffer():
    voice_n = ms_to_frames(500)
    silence_n = ms_to_frames(800)
    probs = [0.9] * voice_n + [0.1] * silence_n
    ep = make_endpointer(probs)
    # Feed some voice
    ep.feed(voiced_frames(voice_n))
    # Drop
    ep.drop_in_flight()
    # Now feed silence — should NOT emit anything
    emitted = ep.feed(silence_frames(silence_n))
    assert emitted == [], "drop_in_flight should have discarded buffer"


def test_reset_clears_state():
    voice_n = ms_to_frames(500)
    silence_n = ms_to_frames(800)
    probs = [0.9] * voice_n + [0.1] * silence_n
    ep = make_endpointer(probs)
    ep.feed(voiced_frames(voice_n))
    ep.reset()
    emitted = ep.feed(silence_frames(silence_n))
    assert emitted == [], "reset should have cleared state"


def test_silence_only_emits_nothing():
    silence_n = ms_to_frames(2000)
    probs = [0.05] * silence_n
    ep = make_endpointer(probs)
    emitted = ep.feed(silence_frames(silence_n))
    assert emitted == []


def test_includes_pre_roll():
    """Voice should include pre-roll frames captured during silence."""
    pre_silence_n = ms_to_frames(500)  # > pre_roll_ms (200)
    voice_n = ms_to_frames(400)
    post_silence_n = ms_to_frames(800)
    probs = [0.05] * pre_silence_n + [0.9] * voice_n + [0.1] * post_silence_n
    ep = make_endpointer(
        probs,
        pre_roll_ms=200,
    )
    emitted = ep.feed(
        silence_frames(pre_silence_n) + voiced_frames(voice_n) + silence_frames(post_silence_n)
    )
    assert len(emitted) == 1
    duration_ms = (len(emitted[0]) * 1000) // (SAMPLE_RATE * 2)
    # Voice (400) + pre-roll (200) + post-roll (300) ≈ 900 ms
    assert 700 <= duration_ms <= 1100, f"unexpected duration {duration_ms}ms"


def test_partial_chunks_handled():
    """Audio fed in arbitrary-sized chunks should still yield correct emission."""
    voice_n = ms_to_frames(400)
    silence_n = ms_to_frames(800)
    probs = [0.9] * voice_n + [0.1] * silence_n
    ep = make_endpointer(probs)
    full = voiced_frames(voice_n) + silence_frames(silence_n)
    emitted = []
    # Feed in odd-sized chunks (not frame-aligned)
    cursor = 0
    while cursor < len(full):
        chunk = full[cursor:cursor + 717]
        emitted.extend(ep.feed(chunk))
        cursor += 717
    assert len(emitted) == 1


def test_env_var_overrides(monkeypatch):
    monkeypatch.setenv("BUSH_VAD_MIN_SILENCE_MS", "200")
    # Re-import to pick up new env var
    import importlib
    import bush_stt.vad as vad_mod
    importlib.reload(vad_mod)
    assert vad_mod.DEFAULT_MIN_SILENCE_MS == 200
    # restore for other tests
    monkeypatch.delenv("BUSH_VAD_MIN_SILENCE_MS", raising=False)
    importlib.reload(vad_mod)


def test_close_is_idempotent():
    ep = make_endpointer([0.0])
    ep.close()
    ep.close()

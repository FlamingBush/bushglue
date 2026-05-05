"""Unit tests for RnnoiseFilter."""
import numpy as np
import pytest

from bush_stt.denoise import RnnoiseFilter, FRAME_SAMPLES, FRAME_BYTES


class EchoFilter:
    """Test stub: echoes input through unchanged."""
    def process_frame(self, arr):
        return arr


class GainFilter:
    """Test stub: halves amplitude (simulates a denoiser doing work)."""
    def __init__(self):
        self.calls = 0
    def process_frame(self, arr):
        self.calls += 1
        return (arr // 2).astype(np.int16)


def make_filter(stub_factory=EchoFilter, **kwargs):
    return RnnoiseFilter(loader=stub_factory, **kwargs)


def silence_bytes(n_frames: int) -> bytes:
    return b"\x00" * (FRAME_BYTES * n_frames)


def tone_bytes(n_frames: int, amplitude: int = 5000) -> bytes:
    """Generate a sine tone in int16 LE bytes (n_frames * 480 samples)."""
    n = FRAME_SAMPLES * n_frames
    t = np.arange(n)
    sig = (amplitude * np.sin(2 * np.pi * 440.0 * t / 48000.0)).astype(np.int16)
    return sig.tobytes()


def test_pass_through_when_disabled():
    f = RnnoiseFilter(enabled=False)
    inp = tone_bytes(2)
    assert f.process(inp) == inp


def test_frame_aligned_passes_through_echo():
    f = make_filter()
    inp = tone_bytes(2)
    assert f.process(inp) == inp


def test_partial_frame_buffered():
    f = make_filter()
    # Send half a frame
    inp = tone_bytes(1)[:FRAME_BYTES // 2]
    assert f.process(inp) == b""  # nothing emitted; buffered
    # Send the other half + one full frame
    rest = tone_bytes(1)[FRAME_BYTES // 2:] + tone_bytes(1)
    out = f.process(rest)
    assert len(out) == FRAME_BYTES * 2


def test_flush_drains_partial_with_zero_pad():
    f = make_filter()
    half = tone_bytes(1)[:FRAME_BYTES // 2]
    assert f.process(half) == b""
    drained = f.flush()
    assert len(drained) == FRAME_BYTES  # zero-padded to full frame


def test_filter_actually_runs():
    f = make_filter(stub_factory=GainFilter)
    inp = tone_bytes(3, amplitude=10000)
    out = f.process(inp)
    assert len(out) == len(inp)
    # GainFilter halves amplitude → output max < input max
    inp_max = np.frombuffer(inp, dtype=np.int16).max()
    out_max = np.frombuffer(out, dtype=np.int16).max()
    assert out_max < inp_max


def test_reset_clears_partial_buffer():
    f = make_filter()
    half = tone_bytes(1)[:FRAME_BYTES // 2]
    f.process(half)
    f.reset()
    # Nothing should be left
    assert f.flush() == b""


def test_close_blocks_further_use():
    f = make_filter()
    f.close()
    with pytest.raises(RuntimeError, match="closed"):
        f.process(tone_bytes(1))


def test_close_is_idempotent():
    f = make_filter()
    f.close()
    f.close()  # should not raise


def test_env_var_disable(monkeypatch):
    monkeypatch.setenv("BUSH_RNNOISE_ENABLED", "0")
    import importlib
    import bush_stt.denoise as d
    importlib.reload(d)
    assert d.DEFAULT_ENABLED is False
    monkeypatch.delenv("BUSH_RNNOISE_ENABLED", raising=False)
    importlib.reload(d)

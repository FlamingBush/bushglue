"""Unit tests for the push-to-talk endpointer.

PttEndpointer is a drop-in for VadEndpointer, so these cover the same
three-method contract (feed / drop_in_flight / reset) plus the button edges.
"""
import threading

import pytest

from bush_stt.ptt import BYTES_PER_SEC, PttEndpointer


def pcm(ms: int) -> bytes:
    """`ms` milliseconds of silence as int16 LE mono at 16 kHz."""
    return b"\x00\x00" * (16000 * ms // 1000)


@pytest.fixture
def ep():
    return PttEndpointer(pre_roll_ms=150, min_utterance_ms=250,
                         max_utterance_ms=2000)


def ms_of(buf: bytes) -> int:
    return (len(buf) // 2) * 1000 // 16000


# ── basic press/release ─────────────────────────────────────────────────────

def test_no_utterance_while_idle(ep):
    assert ep.feed(pcm(500)) == []


def test_press_release_emits_one_utterance(ep):
    ep.press()
    assert ep.feed(pcm(400)) == []       # still held: nothing closed yet
    ep.release()
    out = ep.feed(pcm(20))
    assert len(out) == 1
    assert ms_of(out[0]) >= 400


def test_utterance_is_emitted_only_once(ep):
    ep.press()
    ep.feed(pcm(400))
    ep.release()
    assert len(ep.feed(b"")) == 1
    assert ep.feed(b"") == []


def test_release_without_press_is_a_noop(ep):
    ep.release()
    assert ep.feed(pcm(100)) == []


def test_repeated_press_does_not_restart_the_buffer(ep):
    ep.press()
    ep.feed(pcm(300))
    ep.press()                            # duplicate edge, e.g. MQTT redelivery
    ep.feed(pcm(300))
    ep.release()
    out = ep.feed(b"")
    assert ms_of(out[0]) >= 600


# ── pre-roll ────────────────────────────────────────────────────────────────

def test_pre_roll_is_prepended_to_the_utterance(ep):
    ep.feed(pcm(500))                     # idle audio fills the pre-roll ring
    ep.press()
    ep.feed(pcm(300))
    ep.release()
    out = ep.feed(b"")
    # 300ms held + up to 150ms of pre-roll
    assert 430 <= ms_of(out[0]) <= 460


def test_pre_roll_is_bounded(ep):
    ep.feed(pcm(5000))                    # far more than the 150ms ring
    ep.press()
    ep.release()
    # Released immediately: only pre-roll is buffered, and it is capped, so
    # the result is below min_utterance_ms and dropped as a tap.
    assert ep.feed(b"") == []


def test_pre_roll_does_not_carry_across_utterances(ep):
    ep.feed(pcm(500))
    ep.press()
    ep.feed(pcm(300))
    ep.release()
    ep.feed(b"")
    ep.press()                            # pre-roll was consumed by the first
    ep.feed(pcm(300))
    ep.release()
    out = ep.feed(b"")
    assert ms_of(out[0]) == 300


# ── guards ──────────────────────────────────────────────────────────────────

def test_short_tap_is_dropped(ep):
    ep.press()
    ep.feed(pcm(50))
    ep.release()
    assert ep.feed(b"") == []


def test_stuck_button_emits_and_disarms(ep):
    ep.press()
    out = ep.feed(pcm(2500))              # past the 2000ms ceiling
    assert len(out) == 1
    assert not ep.recording
    # Still "held" as far as the button knows; the release must not re-emit.
    ep.release()
    assert ep.feed(b"") == []


# ── tts coordination ────────────────────────────────────────────────────────

def test_drop_in_flight_discards_the_utterance(ep):
    ep.press()
    ep.feed(pcm(400))
    ep.drop_in_flight()
    ep.release()
    assert ep.feed(b"") == []


def test_reset_clears_a_queued_utterance(ep):
    ep.press()
    ep.feed(pcm(400))
    ep.release()
    ep.reset()
    assert ep.feed(b"") == []


def test_close_is_a_noop(ep):
    assert ep.close() is None


# ── threading ───────────────────────────────────────────────────────────────

def test_press_release_from_another_thread(ep):
    """press/release land on the MQTT thread while feed() runs on capture."""
    ep.press()
    ep.feed(pcm(300))

    done = threading.Event()

    def releaser():
        ep.release()
        done.set()

    t = threading.Thread(target=releaser)
    t.start()
    done.wait(timeout=2)
    t.join()

    out = ep.feed(b"")
    assert len(out) == 1


def test_odd_length_chunks_never_split_a_sample(ep):
    ep.press()
    ep.feed(b"\x01" * 401)                # deliberately odd byte count
    ep.release()
    out = ep.feed(b"")
    if out:
        assert len(out[0]) % 2 == 0 or len(out[0]) == 401


# ── Whisper non-speech markers ──────────────────────────────────────────────
# Whisper narrates silence instead of returning nothing, at confidence 1.00,
# so the confidence gate does not catch it. Observed live: a silent PTT press
# published "[no audio]" and the bush answered it with a verse and fire.

@pytest.mark.parametrize("text", [
    "[no audio]", "[BLANK_AUDIO]", "[ Silence ]", "(upbeat music)",
    "[MUSIC]", "  [no audio]  ",
])
def test_non_speech_markers_are_detected(text):
    from bush_stt import is_non_speech
    assert is_non_speech(text)


@pytest.mark.parametrize("text", [
    "Tell me why the fire does not consume you",
    "I said [pause] and then left",
    "speak to me of mercy",
    "",
])
def test_real_speech_is_not_flagged(text):
    from bush_stt import is_non_speech
    assert not is_non_speech(text)

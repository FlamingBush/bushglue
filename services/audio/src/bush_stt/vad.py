"""Silero VAD endpointer.

Consumes 16 kHz mono int16 LE PCM in arbitrary chunks, emits complete utterance
buffers when a voice→silence transition is detected (or when max-utterance is hit).

State machine
=============

    SILENT (recording rolling pre-roll buffer)
       │
       │  VAD prob > THRESHOLD on a frame
       ▼
    VOICED (collecting utterance buffer)
       │
       │  VAD prob < THRESHOLD sustained for min_silence_to_end_ms
       ▼
    EMIT (flush utterance + post_roll if duration ≥ min_utterance_ms; else discard)
       │
       ▼
    SILENT (start over)

Force-cut: if VOICED duration ≥ max_utterance_ms, emit immediately. If voice is
still active after force-cut, re-enter VOICED with a fresh buffer.

Reset semantics
===============

drop_in_flight() — discard current utterance buffer; called on bush/pipeline/tts/speaking.
                   Returns to SILENT with empty pre-roll.
reset()          — full state reset including pre-roll buffer; called on bush/pipeline/tts/done.
                   Returns to SILENT with empty pre-roll.
"""
from __future__ import annotations

import collections
import os
from typing import Callable, Optional

import numpy as np


# ── tunables (env-overridable) ────────────────────────────────────────────────

def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    return int(raw) if raw and raw.lstrip("-").isdigit() else default


# Locked defaults from eng review D3 — do not change without team review
DEFAULT_MIN_SILENCE_MS = _env_int("BUSH_VAD_MIN_SILENCE_MS", 600)
DEFAULT_MAX_UTTERANCE_MS = _env_int("BUSH_VAD_MAX_UTTERANCE_MS", 15000)
DEFAULT_PRE_ROLL_MS = _env_int("BUSH_VAD_PRE_ROLL_MS", 200)
DEFAULT_POST_ROLL_MS = _env_int("BUSH_VAD_POST_ROLL_MS", 300)
DEFAULT_MIN_UTTERANCE_MS = _env_int("BUSH_VAD_MIN_UTTERANCE_MS", 250)
DEFAULT_THRESHOLD = float(os.environ.get("BUSH_VAD_THRESHOLD", "0.5"))

SAMPLE_RATE = 16000
FRAME_SAMPLES = 512  # Silero's required frame size at 16 kHz
FRAME_BYTES = FRAME_SAMPLES * 2  # int16 LE
FRAME_MS = FRAME_SAMPLES * 1000 // SAMPLE_RATE  # 32 ms


def _ms_to_bytes(ms: int) -> int:
    return (ms * SAMPLE_RATE * 2) // 1000


def log(msg: str) -> None:
    print(f"[vad] {msg}", flush=True)


# ── model loading ─────────────────────────────────────────────────────────────

def _default_load_model():
    """Load Silero VAD. Returns a callable
    (audio: torch.Tensor|ndarray, sample_rate: int) -> tensor|scalar of probs.

    Backend selected by VAD_BACKEND env var: "torch" (default) or "rknn".
    """
    backend = os.environ.get("VAD_BACKEND", "torch").lower()
    if backend == "rknn":
        from bush_stt.engines.silero_rknn import load_silero_vad_rknn
        log("backend: rknn")
        return load_silero_vad_rknn()
    if backend != "torch":
        raise RuntimeError(f"Unknown VAD_BACKEND={backend!r}; must be torch|rknn")
    from silero_vad import load_silero_vad
    log("backend: torch")
    return load_silero_vad()


# ── endpointer ────────────────────────────────────────────────────────────────

class VadEndpointer:
    """Stateful VAD endpointer. NOT thread-safe — call from one thread only."""

    def __init__(
        self,
        *,
        min_silence_ms: int = DEFAULT_MIN_SILENCE_MS,
        max_utterance_ms: int = DEFAULT_MAX_UTTERANCE_MS,
        pre_roll_ms: int = DEFAULT_PRE_ROLL_MS,
        post_roll_ms: int = DEFAULT_POST_ROLL_MS,
        min_utterance_ms: int = DEFAULT_MIN_UTTERANCE_MS,
        threshold: float = DEFAULT_THRESHOLD,
        model_loader: Optional[Callable] = None,
    ):
        self.min_silence_ms = min_silence_ms
        self.max_utterance_ms = max_utterance_ms
        self.pre_roll_ms = pre_roll_ms
        self.post_roll_ms = post_roll_ms
        self.min_utterance_ms = min_utterance_ms
        self.threshold = threshold

        loader = model_loader or _default_load_model
        self._model = loader()

        # Rolling buffer for pre-roll. We keep it as a deque of frames (bytes).
        pre_roll_frames = max(1, self.pre_roll_ms // FRAME_MS)
        self._pre_roll: "collections.deque[bytes]" = collections.deque(maxlen=pre_roll_frames)

        # Frame-aligned buffer for incomplete chunks coming from the caller.
        self._partial: bytearray = bytearray()

        # Active utterance state (only populated when state == VOICED or trailing post-roll)
        self._utterance: bytearray = bytearray()
        self._silence_run_ms: int = 0
        self._post_roll_ms_collected: int = 0
        self._voiced: bool = False
        # Cumulative ms of frames where prob >= threshold (excludes pre-roll
        # and trailing silence). Used for min_utterance_ms gating to filter
        # short noise events regardless of how much pre/post-roll padding is
        # included in the emitted buffer.
        self._voice_ms: int = 0

        log(f"init: thresholds min_silence={min_silence_ms}ms max_utt={max_utterance_ms}ms "
            f"pre_roll={pre_roll_ms}ms post_roll={post_roll_ms}ms min_utt={min_utterance_ms}ms")

    # ── public API ────────────────────────────────────────────────────────────

    def feed(self, audio_chunk: bytes) -> list[bytes]:
        """Feed audio (any length); returns list of complete utterance PCM byte buffers."""
        emitted: list[bytes] = []
        self._partial.extend(audio_chunk)

        while len(self._partial) >= FRAME_BYTES:
            frame_bytes = bytes(self._partial[:FRAME_BYTES])
            del self._partial[:FRAME_BYTES]
            utt = self._consume_frame(frame_bytes)
            if utt is not None:
                emitted.append(utt)
        return emitted

    def drop_in_flight(self) -> None:
        """Discard current utterance + pre-roll. Used on tts/speaking."""
        if self._voiced or self._utterance:
            log("drop_in_flight: discarding in-flight utterance")
        self._utterance.clear()
        self._silence_run_ms = 0
        self._post_roll_ms_collected = 0
        self._voiced = False
        self._voice_ms = 0
        # Clear pre-roll too: we're about to mute, audio captured up to this
        # point is contaminated by what triggered the TTS.
        self._pre_roll.clear()
        self._partial.clear()
        self._reset_model_states()

    def reset(self) -> None:
        """Full state reset. Used on tts/done."""
        log("reset")
        self._utterance.clear()
        self._silence_run_ms = 0
        self._post_roll_ms_collected = 0
        self._voiced = False
        self._voice_ms = 0
        self._pre_roll.clear()
        self._partial.clear()
        self._reset_model_states()

    def _reset_model_states(self) -> None:
        """Clear Silero's LSTM hidden state so it doesn't carry across utterances."""
        reset_fn = getattr(self._model, "reset_states", None)
        if callable(reset_fn):
            reset_fn()

    def close(self) -> None:
        """Release model resources. Idempotent."""
        self._model = None  # let GC handle it

    # ── inner loop ────────────────────────────────────────────────────────────

    def _consume_frame(self, frame_bytes: bytes) -> Optional[bytes]:
        prob = self._frame_voice_prob(frame_bytes)

        if not self._voiced:
            if prob >= self.threshold:
                # Transition SILENT → VOICED. Seed utterance with pre-roll
                # buffer (which does NOT yet contain the current frame) plus
                # the current frame.
                self._voiced = True
                self._silence_run_ms = 0
                self._post_roll_ms_collected = 0
                self._voice_ms = FRAME_MS
                self._utterance.clear()
                for prf in self._pre_roll:
                    self._utterance.extend(prf)
                self._utterance.extend(frame_bytes)
                log("state: silent -> voiced")
                # Now that the frame is part of the utterance, drop the
                # pre-roll buffer until the next utterance.
                self._pre_roll.clear()
            else:
                # SILENT: keep current frame in rolling pre-roll
                self._pre_roll.append(frame_bytes)
            return None

        # VOICED branch
        self._utterance.extend(frame_bytes)

        if prob >= self.threshold:
            # Voice still active: reset silence run, accumulate voice time
            self._silence_run_ms = 0
            self._post_roll_ms_collected = 0
            self._voice_ms += FRAME_MS
        else:
            # Trailing silence: count it for both silence-end and post-roll-collected
            self._silence_run_ms += FRAME_MS
            self._post_roll_ms_collected += FRAME_MS

        # Force-cut on max utterance length
        utt_ms = self._utterance_duration_ms()
        if utt_ms >= self.max_utterance_ms:
            log(f"force-cut at {utt_ms}ms")
            return self._emit_and_reset_voiced(prob)

        # End-of-utterance on sustained silence
        if self._silence_run_ms >= self.min_silence_ms:
            return self._emit_and_reset_voiced(prob)

        return None

    def _emit_and_reset_voiced(self, last_prob: float) -> Optional[bytes]:
        """Emit current utterance if it meets min duration. Reset to SILENT (or VOICED if force-cut while still voiced)."""
        # The utterance buffer already has trailing silence frames included up to
        # post_roll_ms_collected; cap to post_roll_ms.
        post_excess_ms = max(0, self._post_roll_ms_collected - self.post_roll_ms)
        post_excess_bytes = _ms_to_bytes(post_excess_ms)
        # Trim from the end if we collected more silence than post_roll allows
        if post_excess_bytes > 0 and len(self._utterance) > post_excess_bytes:
            del self._utterance[-post_excess_bytes:]

        utt_ms = self._utterance_duration_ms()
        voice_ms = self._voice_ms
        emitted: Optional[bytes] = None

        # Filter on actual voice content, not buffer length (buffer length
        # always includes pre/post roll which would always exceed min_utt_ms).
        if voice_ms >= self.min_utterance_ms:
            emitted = bytes(self._utterance)
            log(f"emit: {utt_ms}ms utterance ({voice_ms}ms voiced, {len(emitted)} bytes)")
        else:
            log(f"discard short utterance ({voice_ms}ms voiced < {self.min_utterance_ms}ms)")

        self._utterance.clear()
        self._silence_run_ms = 0
        self._post_roll_ms_collected = 0
        self._voice_ms = 0
        # If voice was still active at force-cut time, transition back into VOICED.
        # Otherwise, return to SILENT.
        if last_prob >= self.threshold:
            self._voiced = True
            self._voice_ms = FRAME_MS
            log("state: re-voiced after force-cut")
        else:
            self._voiced = False
            log("state: voiced -> silent")
        return emitted

    def _utterance_duration_ms(self) -> int:
        return (len(self._utterance) * 1000) // (SAMPLE_RATE * 2)

    def _frame_voice_prob(self, frame_bytes: bytes) -> float:
        """Run Silero VAD on one 512-sample frame, return voice probability in [0,1]."""
        arr = np.frombuffer(frame_bytes, dtype=np.int16).astype(np.float32) / 32768.0
        try:
            import torch
            audio_in = torch.from_numpy(arr)
            # inference_mode is required: without it Silero's LSTM accumulates
            # autograd graph through its hidden-state buffers across forward
            # calls, leaking ~27 KB per frame (~50 MB/min at 31 fps).
            with torch.inference_mode():
                result = self._model(audio_in, SAMPLE_RATE)
        except ImportError:
            audio_in = arr
            result = self._model(audio_in, SAMPLE_RATE)
        if hasattr(result, "item"):
            result = result.item()
        return float(result)

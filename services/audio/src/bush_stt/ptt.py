"""Push-to-talk endpointer — a drop-in replacement for VadEndpointer.

Same three-method surface (feed / drop_in_flight / reset), but utterance
boundaries come from a physical button instead of Silero. press() opens an
utterance; release() closes it and the buffered PCM comes back from the next
feed() call.

The button knows something the VAD has to infer: exactly when the speaker
started and stopped. That removes both the onset clipping seen on the live
capture path and the min_silence tail latency before an utterance is emitted.

Unlike VadEndpointer this IS thread-safe: press/release arrive on the MQTT
callback thread while feed() runs on the capture thread.

Sizes are in bytes of signed 16-bit LE mono PCM at SAMPLE_RATE.
"""
from __future__ import annotations

import os
import threading


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    return int(raw) if raw and raw.lstrip("-").isdigit() else default


SAMPLE_RATE = 16000
BYTES_PER_SEC = SAMPLE_RATE * 2

# Insurance against a talker who starts a syllable before the switch closes.
DEFAULT_PRE_ROLL_MS = _env_int("BUSH_PTT_PRE_ROLL_MS", 150)

# Taps shorter than this are fumbles, not utterances. bush-ptt debounces
# contact bounce at ~25 ms; this is the separate, semantic filter.
DEFAULT_MIN_UTTERANCE_MS = _env_int("BUSH_PTT_MIN_UTTERANCE_MS", 250)

# A stuck button or a shorted wire must not buffer without bound. On reaching
# this the utterance is emitted and recording stops until the next press.
DEFAULT_MAX_UTTERANCE_MS = _env_int("BUSH_PTT_MAX_UTTERANCE_MS", 30000)


def log(msg: str) -> None:
    print(f"[ptt-endpoint] {msg}", flush=True)


class PttEndpointer:
    """Button-driven endpointer. Thread-safe."""

    def __init__(
        self,
        *,
        pre_roll_ms: int = DEFAULT_PRE_ROLL_MS,
        min_utterance_ms: int = DEFAULT_MIN_UTTERANCE_MS,
        max_utterance_ms: int = DEFAULT_MAX_UTTERANCE_MS,
    ):
        self.pre_roll_ms = pre_roll_ms
        self.min_utterance_ms = min_utterance_ms
        self.max_utterance_ms = max_utterance_ms

        self._pre_roll_bytes = self._ms_to_bytes(pre_roll_ms)
        self._max_bytes = self._ms_to_bytes(max_utterance_ms)

        self._lock = threading.Lock()
        self._recording = False
        self._buf = bytearray()
        self._pre_roll = bytearray()
        self._pending: list[bytes] = []

        log(f"init: pre_roll={pre_roll_ms}ms min_utt={min_utterance_ms}ms "
            f"max_utt={max_utterance_ms}ms")

    # ── button edges (MQTT thread) ────────────────────────────────────────

    def press(self) -> None:
        """Open an utterance, seeded with whatever pre-roll is buffered."""
        with self._lock:
            if self._recording:
                return
            self._recording = True
            self._buf = bytearray(self._pre_roll)
            self._pre_roll = bytearray()

    def release(self) -> None:
        """Close the utterance and queue it for the next feed()."""
        with self._lock:
            if not self._recording:
                return
            self._recording = False
            utt = bytes(self._buf)
            self._buf = bytearray()

            ms = self._bytes_to_ms(len(utt))
            if ms < self.min_utterance_ms:
                log(f"ignoring {ms}ms tap (< {self.min_utterance_ms}ms)")
                return
            self._pending.append(utt)

    # ── endpointer API (capture thread) ───────────────────────────────────

    def feed(self, audio_chunk: bytes) -> list[bytes]:
        """Buffer audio; return any utterances closed since the last call."""
        with self._lock:
            if self._recording:
                self._buf.extend(audio_chunk)
                if len(self._buf) >= self._max_bytes:
                    log(f"max utterance {self.max_utterance_ms}ms reached — "
                        f"emitting and disarming (button stuck?)")
                    self._pending.append(bytes(self._buf))
                    self._buf = bytearray()
                    self._recording = False
            else:
                self._pre_roll.extend(audio_chunk)
                excess = len(self._pre_roll) - self._pre_roll_bytes
                if excess > 0:
                    del self._pre_roll[:excess]

            emitted, self._pending = self._pending, []
            return emitted

    def drop_in_flight(self) -> None:
        """Discard the in-flight utterance and pre-roll. Used on tts/speaking."""
        with self._lock:
            if self._recording or self._buf:
                log("drop_in_flight: discarding in-flight utterance")
            self._recording = False
            self._buf = bytearray()
            self._pre_roll = bytearray()

    def reset(self) -> None:
        """Full state reset. Used on tts/done."""
        with self._lock:
            log("reset")
            self._recording = False
            self._buf = bytearray()
            self._pre_roll = bytearray()
            self._pending = []

    def close(self) -> None:
        """Interface parity with VadEndpointer; nothing to release."""
        return None

    # ── helpers ───────────────────────────────────────────────────────────

    @property
    def recording(self) -> bool:
        with self._lock:
            return self._recording

    @staticmethod
    def _ms_to_bytes(ms: int) -> int:
        # Round to a whole sample so the buffer never splits an int16.
        return (ms * BYTES_PER_SEC // 1000) & ~1

    @staticmethod
    def _bytes_to_ms(n: int) -> int:
        return (n // 2) * 1000 // SAMPLE_RATE

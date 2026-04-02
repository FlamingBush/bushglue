"""
Unit tests for speech-to-text/transcriber.py

Run from the bushglue/ directory:
    python3 -m pytest tests/test_transcriber.py -v
or:
    python3 tests/test_transcriber.py

Vosk is mocked so no model files are needed.
"""
import json
import sys
import unittest
from unittest.mock import MagicMock, patch

# ---------------------------------------------------------------------------
# Stub out vosk before importing transcriber so tests run without Vosk installed
# ---------------------------------------------------------------------------
vosk_stub = MagicMock()
vosk_stub.Model = MagicMock
vosk_stub.KaldiRecognizer = MagicMock
sys.modules.setdefault("vosk", vosk_stub)

# transcriber lives in speech-to-text/; add it to the path
import os
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(_REPO_ROOT, "speech-to-text"))

from transcriber import SpeechToText  # noqa: E402


def _make_stt() -> SpeechToText:
    """Return a SpeechToText with a mocked Vosk recognizer."""
    stt = SpeechToText.__new__(SpeechToText)
    stt.model = MagicMock()
    stt.recognizer = MagicMock()
    return stt


class TestAcceptAudioFinalResult(unittest.TestCase):
    def test_final_result_returned(self):
        stt = _make_stt()
        stt.recognizer.AcceptWaveform.return_value = True
        stt.recognizer.Result.return_value = json.dumps({"text": "burning bush"})
        result = stt.accept_audio(b"\x00" * 16)
        self.assertEqual(result, {"type": "final", "text": "burning bush"})

    def test_final_result_strips_whitespace(self):
        stt = _make_stt()
        stt.recognizer.AcceptWaveform.return_value = True
        stt.recognizer.Result.return_value = json.dumps({"text": "  what is fire  "})
        result = stt.accept_audio(b"\x00" * 16)
        self.assertEqual(result["text"], "what is fire")

    def test_final_empty_text(self):
        stt = _make_stt()
        stt.recognizer.AcceptWaveform.return_value = True
        stt.recognizer.Result.return_value = json.dumps({"text": ""})
        result = stt.accept_audio(b"\x00" * 16)
        self.assertEqual(result, {"type": "final", "text": ""})

    def test_final_json_error_returns_error_type(self):
        """Malformed JSON from Vosk must not crash the recognizer thread."""
        stt = _make_stt()
        stt.recognizer.AcceptWaveform.return_value = True
        stt.recognizer.Result.return_value = "not json {"
        result = stt.accept_audio(b"\x00" * 16)
        self.assertEqual(result["type"], "error")
        self.assertEqual(result["text"], "")

    def test_final_recognizer_exception_returns_error_type(self):
        """If AcceptWaveform itself raises, we should get an error type."""
        stt = _make_stt()
        stt.recognizer.AcceptWaveform.side_effect = RuntimeError("vosk exploded")
        result = stt.accept_audio(b"\x00" * 16)
        self.assertEqual(result["type"], "error")
        self.assertEqual(result["text"], "")


class TestAcceptAudioPartialResult(unittest.TestCase):
    def test_partial_result_returned(self):
        stt = _make_stt()
        stt.recognizer.AcceptWaveform.return_value = False
        stt.recognizer.PartialResult.return_value = json.dumps({"partial": "speak of the"})
        result = stt.accept_audio(b"\x00" * 16)
        self.assertEqual(result, {"type": "partial", "text": "speak of the"})

    def test_partial_empty(self):
        stt = _make_stt()
        stt.recognizer.AcceptWaveform.return_value = False
        stt.recognizer.PartialResult.return_value = json.dumps({"partial": ""})
        result = stt.accept_audio(b"\x00" * 16)
        self.assertEqual(result, {"type": "partial", "text": ""})

    def test_partial_json_error_returns_error_type(self):
        stt = _make_stt()
        stt.recognizer.AcceptWaveform.return_value = False
        stt.recognizer.PartialResult.return_value = "{{bad"
        result = stt.accept_audio(b"\x00" * 16)
        self.assertEqual(result["type"], "error")
        self.assertEqual(result["text"], "")


class TestFinalResult(unittest.TestCase):
    def test_final_result_normal(self):
        stt = _make_stt()
        stt.recognizer.FinalResult.return_value = json.dumps({"text": "voice in the wilderness"})
        self.assertEqual(stt.final_result(), "voice in the wilderness")

    def test_final_result_empty(self):
        stt = _make_stt()
        stt.recognizer.FinalResult.return_value = json.dumps({"text": ""})
        self.assertEqual(stt.final_result(), "")

    def test_final_result_json_error_returns_empty_string(self):
        """JSON error must not propagate — return '' so callers get a falsy value."""
        stt = _make_stt()
        stt.recognizer.FinalResult.return_value = "not json"
        self.assertEqual(stt.final_result(), "")

    def test_final_result_strips_whitespace(self):
        stt = _make_stt()
        stt.recognizer.FinalResult.return_value = json.dumps({"text": "  desert  "})
        self.assertEqual(stt.final_result(), "desert")


class TestForceFinalizeNoFallback(unittest.TestCase):
    """
    Regression: when force-finalize fires with no speech, no transcript must
    be published. The old code would publish a random fallback phrase.
    """

    def test_no_speech_means_no_publish(self):
        """
        Simulate the force-finalize logic from stt-service: if final_result()
        and last_partial are both empty, the publish path must NOT be reached.
        """
        stt = _make_stt()
        stt.recognizer.FinalResult.return_value = json.dumps({"text": ""})

        last_partial = ""
        text = stt.final_result() or last_partial

        # The key assertion: text is falsy — nothing to publish
        self.assertFalse(text, "Expected no text when Vosk returns empty and no partial exists")

    def test_partial_used_when_final_empty(self):
        stt = _make_stt()
        stt.recognizer.FinalResult.return_value = json.dumps({"text": ""})

        last_partial = "speak of the light"
        text = stt.final_result() or last_partial

        self.assertEqual(text, "speak of the light")

    def test_final_takes_priority_over_partial(self):
        stt = _make_stt()
        stt.recognizer.FinalResult.return_value = json.dumps({"text": "burning bush"})

        last_partial = "partial text"
        text = stt.final_result() or last_partial

        self.assertEqual(text, "burning bush")


if __name__ == "__main__":
    unittest.main(verbosity=2)

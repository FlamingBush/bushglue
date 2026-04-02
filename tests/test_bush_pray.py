"""
Tests for utils/bush-pray — filename sanitization and synthesis argument
construction. No audio hardware, espeak-ng, or sox required.

Run from bushglue/:
    python -m pytest tests/test_bush_pray.py -v
"""
import importlib.util
import os
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

_BUSHGLUE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_BUSH_PRAY = os.path.join(_BUSHGLUE, "utils", "bush-pray")

# Load bush-pray (no .py extension) as a module
from importlib.machinery import SourceFileLoader  # noqa: E402
_loader = SourceFileLoader("bush_pray", _BUSH_PRAY)
_spec = importlib.util.spec_from_loader("bush_pray", _loader)
assert _spec is not None
bush_pray = importlib.util.module_from_spec(_spec)
assert _spec.loader is not None
_spec.loader.exec_module(bush_pray)  # type: ignore[union-attr]


# ── phrase_to_filename ───────────────────────────────────────────────────────

class TestPhraseToFilename(unittest.TestCase):

    def test_basic_phrase(self):
        self.assertEqual(bush_pray.phrase_to_filename("hello world"), "hello_world.wav")

    def test_uppercase_lowercased(self):
        self.assertEqual(bush_pray.phrase_to_filename("FIRE"), "fire.wav")

    def test_special_chars_stripped(self):
        name = bush_pray.phrase_to_filename("what is the meaning of fire?!")
        self.assertNotIn("?", name)
        self.assertNotIn("!", name)
        self.assertTrue(name.endswith(".wav"))

    def test_multiple_spaces_collapsed(self):
        name = bush_pray.phrase_to_filename("  lots   of   spaces  ")
        self.assertNotIn("__", name)

    def test_no_leading_or_trailing_underscores(self):
        name = bush_pray.phrase_to_filename("  hello  ")
        base = name[:-4]  # strip .wav
        self.assertFalse(base.startswith("_"))
        self.assertFalse(base.endswith("_"))

    def test_punctuation_only_becomes_wav(self):
        # edge case: only special chars → slug is empty → just ".wav"
        name = bush_pray.phrase_to_filename("???")
        self.assertTrue(name.endswith(".wav"))

    def test_same_phrase_same_filename(self):
        a = bush_pray.phrase_to_filename("what is fire")
        b = bush_pray.phrase_to_filename("what is fire")
        self.assertEqual(a, b)

    def test_different_phrases_different_filenames(self):
        a = bush_pray.phrase_to_filename("fire")
        b = bush_pray.phrase_to_filename("water")
        self.assertNotEqual(a, b)


# ── synthesize ───────────────────────────────────────────────────────────────

class TestSynthesize(unittest.TestCase):

    def test_espeak_called_with_phrase(self):
        fake_proc = MagicMock()
        fake_proc.stdout = MagicMock()

        with patch("subprocess.Popen", return_value=fake_proc) as mock_popen, \
             patch("subprocess.run"):
            bush_pray.synthesize("burning bush", Path("/tmp/test.wav"))

        espeak_call = mock_popen.call_args_list[0]
        cmd = espeak_call.args[0]
        self.assertIn("espeak-ng", cmd)
        self.assertIn("burning bush", cmd)

    def test_sox_receives_espeak_stdout_as_stdin(self):
        fake_proc = MagicMock()
        fake_proc.stdout = MagicMock()

        with patch("subprocess.Popen", return_value=fake_proc), \
             patch("subprocess.run") as mock_run:
            bush_pray.synthesize("test phrase", Path("/tmp/out.wav"))

        sox_call = mock_run.call_args
        self.assertEqual(sox_call.kwargs.get("stdin"), fake_proc.stdout)

    def test_output_path_passed_to_sox(self):
        fake_proc = MagicMock()
        fake_proc.stdout = MagicMock()
        out = Path("/tmp/custom_output.wav")

        with patch("subprocess.Popen", return_value=fake_proc), \
             patch("subprocess.run") as mock_run:
            bush_pray.synthesize("verse", out)

        sox_cmd = mock_run.call_args.args[0]
        self.assertIn(str(out), sox_cmd)


if __name__ == "__main__":
    unittest.main()

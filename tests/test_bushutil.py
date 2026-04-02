"""
Tests for bushutil.py — audio device persistence, settings persistence,
broker discovery, and sox effects chain.

No hardware, audio, or network required.

Run from bushglue/:
    python -m pytest tests/test_bushutil.py -v
"""
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

_BUSHGLUE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _BUSHGLUE)

import bushutil  # noqa: E402


# ── audio device persistence ─────────────────────────────────────────────────

class TestAudioDevicePersistence(unittest.TestCase):

    def setUp(self):
        self._tmp = Path(self._getTestDir())
        self._config = self._tmp / "audio-devices.json"
        patcher = patch.object(bushutil, "_CONFIG_FILE", self._config)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _getTestDir(self):
        import tempfile
        d = tempfile.mkdtemp()
        self.addCleanup(__import__("shutil").rmtree, d)
        return d

    def test_load_returns_none_when_no_file(self):
        self.assertIsNone(bushutil.load_audio_device("stt"))

    def test_save_and_load_roundtrip(self):
        bushutil.save_audio_device("stt", "hw:1,0")
        self.assertEqual(bushutil.load_audio_device("stt"), "hw:1,0")

    def test_save_multiple_keys(self):
        bushutil.save_audio_device("stt", "hw:1,0")
        bushutil.save_audio_device("tts", "hw:2,0")
        self.assertEqual(bushutil.load_audio_device("stt"), "hw:1,0")
        self.assertEqual(bushutil.load_audio_device("tts"), "hw:2,0")

    def test_overwrite_existing_key(self):
        bushutil.save_audio_device("stt", "hw:1,0")
        bushutil.save_audio_device("stt", "hw:3,0")
        self.assertEqual(bushutil.load_audio_device("stt"), "hw:3,0")

    def test_load_missing_key_returns_none(self):
        bushutil.save_audio_device("stt", "hw:1,0")
        self.assertIsNone(bushutil.load_audio_device("tts"))

    def test_load_corrupt_file_returns_none(self):
        self._config.parent.mkdir(parents=True, exist_ok=True)
        self._config.write_text("not json {{{")
        self.assertIsNone(bushutil.load_audio_device("stt"))


# ── settings persistence ─────────────────────────────────────────────────────

class TestSettingsPersistence(unittest.TestCase):

    def setUp(self):
        import tempfile
        import shutil
        self._tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, str(self._tmp))
        self._settings = self._tmp / "settings.json"
        patcher = patch.object(bushutil, "_SETTINGS_FILE", self._settings)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_load_returns_default_when_missing(self):
        self.assertEqual(bushutil.load_setting("clarity", 50), 50)

    def test_save_and_load_roundtrip(self):
        bushutil.save_setting("clarity", 75)
        self.assertEqual(bushutil.load_setting("clarity"), 75)

    def test_default_returned_for_absent_key(self):
        bushutil.save_setting("foo", "bar")
        self.assertEqual(bushutil.load_setting("missing", "default_val"), "default_val")


# ── get_mqtt_broker ──────────────────────────────────────────────────────────

class TestGetMqttBroker(unittest.TestCase):

    def test_returns_localhost_on_native_linux(self):
        with patch("builtins.open", unittest.mock.mock_open(read_data="Linux 5.15 native")):
            result = bushutil.get_mqtt_broker()
        self.assertEqual(result, "localhost")

    def test_returns_gateway_on_wsl2(self):
        fake_route = "default via 172.26.160.1 dev eth0\n172.26.0.0/20 dev eth0\n"
        with patch("builtins.open", unittest.mock.mock_open(read_data="Linux microsoft WSL2")):
            with patch("subprocess.run") as mock_run:
                mock_run.return_value = MagicMock(stdout=fake_route)
                result = bushutil.get_mqtt_broker()
        self.assertEqual(result, "172.26.160.1")

    def test_returns_localhost_when_proc_version_unreadable(self):
        with patch("builtins.open", side_effect=OSError("no /proc")):
            result = bushutil.get_mqtt_broker()
        self.assertEqual(result, "localhost")

    def test_returns_localhost_when_no_default_route_on_wsl(self):
        with patch("builtins.open", unittest.mock.mock_open(read_data="microsoft wsl")):
            with patch("subprocess.run") as mock_run:
                mock_run.return_value = MagicMock(stdout="192.168.1.0/24 dev eth0\n")
                result = bushutil.get_mqtt_broker()
        self.assertEqual(result, "localhost")


# ── build_sox_effects ────────────────────────────────────────────────────────

class TestBuildSoxEffects(unittest.TestCase):

    def test_returns_list(self):
        self.assertIsInstance(bushutil.build_sox_effects(), list)

    def test_clarity_0_is_default(self):
        default = bushutil.build_sox_effects()
        explicit = bushutil.build_sox_effects(0)
        self.assertEqual(default, explicit)

    def test_contains_reverb(self):
        effects = bushutil.build_sox_effects(0)
        self.assertIn("reverb", effects)

    def test_clarity_100_differs_from_clarity_0(self):
        dramatic = bushutil.build_sox_effects(0)
        clear = bushutil.build_sox_effects(100)
        self.assertNotEqual(dramatic, clear)

    def test_pitch_shift_present(self):
        effects = bushutil.build_sox_effects(0)
        self.assertIn("pitch", effects)

    def test_clarity_midpoint_is_interpolated(self):
        c0 = bushutil.build_sox_effects(0)
        c50 = bushutil.build_sox_effects(50)
        c100 = bushutil.build_sox_effects(100)
        # All three should differ
        self.assertNotEqual(c0, c50)
        self.assertNotEqual(c50, c100)


if __name__ == "__main__":
    unittest.main()

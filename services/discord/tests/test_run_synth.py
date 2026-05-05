"""Test the Discord TTS adapter migration."""
import sys
import types
from unittest.mock import MagicMock

import pytest


def _ensure_discord_stub():
    """Provide a minimal `discord` module stub so bush_discord imports cleanly
    without the real discord.py wheel installed (CI / dev environments)."""
    if "discord" in sys.modules:
        return
    discord_mod = types.ModuleType("discord")

    class _AudioSource:
        pass

    class _Intents:
        @staticmethod
        def default():
            return _Intents()
        message_content = False
        voice_states = False

    class _Client:
        def __init__(self, *a, **kw):
            pass

    discord_mod.AudioSource = _AudioSource
    discord_mod.Intents = _Intents
    discord_mod.Client = _Client
    discord_mod.Embed = MagicMock
    discord_mod.Object = MagicMock
    discord_mod.VoiceClient = MagicMock
    discord_mod.VoiceChannel = MagicMock
    discord_mod.Member = MagicMock
    discord_mod.Message = MagicMock
    discord_mod.Interaction = MagicMock
    discord_mod.VoiceState = MagicMock
    discord_mod.DMChannel = MagicMock
    discord_mod.TextChannel = MagicMock
    discord_mod.Forbidden = type("Forbidden", (Exception,), {})
    discord_mod.utils = types.SimpleNamespace(get=lambda *a, **kw: None)
    discord_mod.opus = types.SimpleNamespace(Decoder=MagicMock)

    app_commands_mod = types.ModuleType("discord.app_commands")
    app_commands_mod.CommandTree = MagicMock
    app_commands_mod.describe = lambda **kw: (lambda f: f)
    app_commands_mod.command = lambda **kw: (lambda f: f)
    discord_mod.app_commands = app_commands_mod

    ext_mod = types.ModuleType("discord.ext")
    discord_mod.ext = ext_mod

    sys.modules["discord"] = discord_mod
    sys.modules["discord.app_commands"] = app_commands_mod
    sys.modules["discord.ext"] = ext_mod
    # discord.ext.voice_recv is optional — leaving absent triggers HAS_VOICE_RECV=False


@pytest.fixture(autouse=True)
def stub_engines(monkeypatch):
    """Stub the engine modules at sys.modules level so imports work without
    real wheels installed."""
    _ensure_discord_stub()

    fake_base = MagicMock()
    fake_base.TTSEngine = type("TTSEngine", (), {})
    fake_espeak = MagicMock()
    fake_piper = MagicMock()
    fake_engines_pkg = MagicMock()
    fake_engines_pkg.base = fake_base
    fake_engines_pkg.espeak = fake_espeak
    fake_engines_pkg.piper = fake_piper

    fake_bush_tts = MagicMock()
    fake_bush_tts.engines = fake_engines_pkg

    monkeypatch.setitem(sys.modules, "bush_tts", fake_bush_tts)
    monkeypatch.setitem(sys.modules, "bush_tts.engines", fake_engines_pkg)
    monkeypatch.setitem(sys.modules, "bush_tts.engines.base", fake_base)
    monkeypatch.setitem(sys.modules, "bush_tts.engines.espeak", fake_espeak)
    monkeypatch.setitem(sys.modules, "bush_tts.engines.piper", fake_piper)
    yield {"espeak": fake_espeak, "piper": fake_piper}


def test_build_engine_defaults_to_espeak(stub_engines, monkeypatch):
    monkeypatch.delenv("TTS_ENGINE", raising=False)
    import importlib
    import bush_discord
    importlib.reload(bush_discord)
    bush_discord._build_tts_engine()
    stub_engines["espeak"].EspeakEngine.assert_called_once()


def test_build_engine_picks_piper(stub_engines, monkeypatch):
    monkeypatch.setenv("TTS_ENGINE", "piper")
    monkeypatch.setenv("PIPER_VOICE", "/fake/voice.onnx")
    import importlib
    import bush_discord
    importlib.reload(bush_discord)
    bush_discord._build_tts_engine()
    stub_engines["piper"].PiperEngine.assert_called_once_with(voice_path="/fake/voice.onnx")


def test_build_engine_unknown_raises(stub_engines, monkeypatch):
    monkeypatch.setenv("TTS_ENGINE", "bogus")
    import importlib
    import bush_discord
    importlib.reload(bush_discord)
    with pytest.raises(RuntimeError, match="Unknown TTS_ENGINE"):
        bush_discord._build_tts_engine()


def test_run_synth_returns_empty_on_engine_error(stub_engines, monkeypatch):
    """If the engine raises, _run_synth returns empty bytes, not a crash."""
    import importlib
    import bush_discord
    importlib.reload(bush_discord)
    fake_engine = MagicMock()
    fake_engine.synthesize.side_effect = RuntimeError("boom")
    monkeypatch.setattr(bush_discord, "_tts_engine", fake_engine)
    src = bush_discord.DiscordTTSSource()
    result = src._run_synth("hello")
    assert result == b""


def test_run_synth_returns_empty_on_empty_audio(stub_engines, monkeypatch):
    import importlib
    import bush_discord
    importlib.reload(bush_discord)
    fake_engine = MagicMock()
    fake_engine.synthesize.return_value = {"audio_pcm": b"", "sample_rate": 22050, "ts": 0.0}
    monkeypatch.setattr(bush_discord, "_tts_engine", fake_engine)
    src = bush_discord.DiscordTTSSource()
    assert src._run_synth("hi") == b""


def test_run_synth_pipes_engine_output_to_sox(stub_engines, monkeypatch):
    """Verify the engine output flows into sox's stdin and sox stdout is returned."""
    import importlib
    import bush_discord
    importlib.reload(bush_discord)
    fake_engine = MagicMock()
    fake_engine.synthesize.return_value = {
        "audio_pcm": b"\x10\x00" * 1000,
        "sample_rate": 22050,
        "ts": 0.0,
    }
    monkeypatch.setattr(bush_discord, "_tts_engine", fake_engine)

    fake_sox = MagicMock()
    fake_sox.communicate.return_value = (b"\xab\xcd" * 500, b"")
    captured = {}
    def fake_popen(cmd, **kwargs):
        captured["cmd"] = cmd
        return fake_sox
    monkeypatch.setattr("subprocess.Popen", fake_popen)
    src = bush_discord.DiscordTTSSource()
    result = src._run_synth("hello")
    assert result == b"\xab\xcd" * 500
    fake_sox.communicate.assert_called_once_with(b"\x10\x00" * 1000, timeout=60)
    cmd = captured["cmd"]
    assert "sox" in cmd[0]
    # input: raw mono at engine sr
    assert cmd[cmd.index("-r") + 1] == "22050"
    # output: stereo 48kHz
    assert "48000" in cmd
    assert "2" in cmd  # stereo

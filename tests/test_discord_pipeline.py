"""
Tests for discord-bot.py — PipelineResult dataclass and build_summary_embed.

Stubs out discord, discord.app_commands, discord.ext.voice_recv, paho.mqtt,
numpy, and sounddevice before importing the module so no hardware or external
services are required.

Run from bushglue/:
    python -m pytest tests/test_discord_pipeline.py -v
"""
import importlib.util
import os
import sys
import types
import unittest

# ── discord stub ──────────────────────────────────────────────────────────────
# Must happen BEFORE the module is loaded.

class _FakeEmbed:
    def __init__(self, title=None, description=None, color=None):
        self.title = title
        self.description = description
        self.color = color
        self.fields: list = []
        self.footer = None

    def add_field(self, name, value, inline=False):
        self.fields.append({"name": name, "value": value, "inline": inline})

    def set_footer(self, text=None):
        self.footer = text


_discord_stub = types.ModuleType("discord")
_app_commands_stub = types.ModuleType("discord.app_commands")
_intents_ns = types.SimpleNamespace(default=lambda: None, message_content=None, voice_states=None)

for _name, _val in [
    ("AudioSource", object),
    ("Embed", _FakeEmbed),
    ("Intents", _intents_ns),
    # Minimal class stubs needed for discord-bot.py class definitions / annotations
    ("Client", object),
    ("VoiceClient", object),
    ("VoiceChannel", object),
    ("Member", object),
    ("VoiceState", object),
    ("Interaction", object),
    ("Message", object),
    ("DMChannel", object),
    ("TextChannel", object),
    ("Forbidden", Exception),
    ("Object", lambda **kw: None),
    ("app_commands", _app_commands_stub),
]:
    setattr(_discord_stub, _name, _val)

for _name, _val in [
    ("CommandTree", object),
    ("command", lambda **kw: (lambda f: f)),
    ("describe", lambda **kw: (lambda f: f)),
]:
    setattr(_app_commands_stub, _name, _val)

sys.modules["discord"] = _discord_stub
sys.modules["discord.app_commands"] = _app_commands_stub

_ext = types.ModuleType("discord.ext")
_voice_recv = types.ModuleType("discord.ext.voice_recv")


# VoiceRecvClient must exist so discord-bot.py's try/except block can patch it.
class _FakeVoiceRecvClient:
    @staticmethod
    def _remove_ssrc(*args, **kwargs):
        pass


setattr(_voice_recv, "BasicSink", object)
setattr(_voice_recv, "VoiceRecvClient", _FakeVoiceRecvClient)
sys.modules["discord.ext"] = _ext
sys.modules["discord.ext.voice_recv"] = _voice_recv

# ── other stubs (paho, numpy, sounddevice) ───────────────────────────────────
if "paho" not in sys.modules:
    _paho = types.ModuleType("paho")
    _paho_mqtt = types.ModuleType("paho.mqtt")
    _paho_mqtt_client = types.ModuleType("paho.mqtt.client")

    class _FakeMQTTClient:
        def __init__(self, *a, **kw): pass
        def connect(self, *a, **kw): pass
        def loop_start(self): pass
        def loop_stop(self): pass
        def publish(self, *a, **kw): pass
        def subscribe(self, *a, **kw): pass
        def on_connect(self): pass
        def on_message(self): pass

    setattr(_paho_mqtt_client, "Client", _FakeMQTTClient)
    setattr(_paho_mqtt_client, "CallbackAPIVersion", types.SimpleNamespace(VERSION2=2))
    sys.modules["paho"] = _paho
    sys.modules["paho.mqtt"] = _paho_mqtt
    sys.modules["paho.mqtt.client"] = _paho_mqtt_client

if "numpy" not in sys.modules:
    import unittest.mock as _mock
    sys.modules["numpy"] = _mock.MagicMock()

if "sounddevice" not in sys.modules:
    import unittest.mock as _mock  # noqa: F811
    sys.modules["sounddevice"] = _mock.MagicMock()

# ── load discord-bot.py ───────────────────────────────────────────────────────
_BUSHGLUE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_BOT_PATH = os.path.join(_BUSHGLUE, "discord-bot.py")

discord_bot = None  # type: ignore[assignment]
_LOAD_ERROR: str = ""

try:
    from importlib.machinery import SourceFileLoader  # noqa: E402
    _loader = SourceFileLoader("discord_bot", _BOT_PATH)
    _spec = importlib.util.spec_from_loader("discord_bot", _loader)
    assert _spec is not None
    discord_bot = importlib.util.module_from_spec(_spec)
    assert _spec.loader is not None
    _spec.loader.exec_module(discord_bot)  # type: ignore[union-attr]
except Exception as exc:
    _LOAD_ERROR = str(exc)

_BOT_LOADED = discord_bot is not None


# ── helpers ───────────────────────────────────────────────────────────────────

def _result(**overrides):
    """Build a minimal valid PipelineResult with sensible defaults."""
    defaults = dict(
        verse="And the fire of the Lord fell",
        transcript="what is fire",
        sentiment=[{"label": "joy", "score": 0.8}, {"label": "sadness", "score": 0.2}],
        stages=[("t2v/verse", "pass", 1.2, 45), ("tts/done", "pass", 8.5, 30)],
        flare_count=2,
        flare_total_ms=3000,
        bigjet_count=1,
        bigjet_total_ms=800,
        total_elapsed_s=12.3,
        passed=True,
    )
    defaults.update(overrides)
    return discord_bot.PipelineResult(**defaults)  # type: ignore[union-attr]


def _get_field(embed, name: str):
    """Return the first field dict whose 'name' key matches, or None."""
    for f in embed.fields:
        if f["name"] == name:
            return f
    return None


# ── TestPipelineResult ────────────────────────────────────────────────────────

@unittest.skipUnless(_BOT_LOADED, f"discord_bot failed to load: {_LOAD_ERROR}")
class TestPipelineResult(unittest.TestCase):

    def test_dataclass_fields(self):
        """All expected fields are present on PipelineResult."""
        import dataclasses
        field_names = {f.name for f in dataclasses.fields(discord_bot.PipelineResult)}  # type: ignore[union-attr]
        expected = {
            "verse", "transcript", "sentiment", "stages",
            "flare_count", "flare_total_ms",
            "bigjet_count", "bigjet_total_ms",
            "total_elapsed_s", "passed",
        }
        self.assertEqual(expected, field_names)

    def test_passed_false_when_set(self):
        r = _result(passed=False)
        self.assertFalse(r.passed)


# ── TestBuildSummaryEmbed ─────────────────────────────────────────────────────

@unittest.skipUnless(_BOT_LOADED, f"discord_bot failed to load: {_LOAD_ERROR}")
class TestBuildSummaryEmbed(unittest.TestCase):

    def _embed(self, **overrides) -> _FakeEmbed:
        r = _result(**overrides)
        return discord_bot.build_summary_embed("what is fire", r)  # type: ignore[union-attr]

    # -- color --

    def test_joy_sentiment_sets_green_color(self):
        embed = self._embed(
            sentiment=[{"label": "joy", "score": 0.9}, {"label": "sadness", "score": 0.1}]
        )
        self.assertEqual(embed.color, discord_bot.EMOTION_COLORS["joy"])  # type: ignore[union-attr]

    def test_anger_sentiment_sets_red_color(self):
        embed = self._embed(
            sentiment=[{"label": "anger", "score": 0.95}, {"label": "joy", "score": 0.05}]
        )
        self.assertEqual(embed.color, discord_bot.EMOTION_COLORS["anger"])  # type: ignore[union-attr]

    def test_no_sentiment_uses_grey(self):
        embed = self._embed(sentiment=None)
        self.assertEqual(embed.color, 0x95A5A6)

    # -- description --

    def test_description_includes_phrase(self):
        r = _result()
        embed = discord_bot.build_summary_embed("burning bush", r)  # type: ignore[union-attr]
        self.assertIn("burning bush", embed.description)

    def test_description_includes_transcript_when_present(self):
        embed = self._embed(transcript="what is fire")
        self.assertIn("what is fire", embed.description)

    def test_description_includes_verse_when_present(self):
        embed = self._embed(verse="And the fire of the Lord fell")
        self.assertIn("And the fire of the Lord fell", embed.description)

    def test_description_omits_transcript_when_none(self):
        embed = self._embed(transcript=None)
        # "heard" label should not appear when transcript is None
        self.assertNotIn("**heard**", embed.description)

    # -- stages field --

    def test_stages_field_present_when_stages_exist(self):
        embed = self._embed()
        field = _get_field(embed, "Stages")
        self.assertIsNotNone(field, "Expected a 'Stages' field in embed")

    def test_passed_stage_shows_checkmark(self):
        embed = self._embed(stages=[("t2v/verse", "pass", 1.2, 45)])
        field = _get_field(embed, "Stages")
        self.assertIsNotNone(field)
        self.assertIn("✅", field["value"])

    def test_failed_stage_shows_cross(self):
        embed = self._embed(stages=[("t2v/verse", "fail", None, 45)])
        field = _get_field(embed, "Stages")
        self.assertIsNotNone(field)
        self.assertIn("❌", field["value"])

    # -- sentiment field --

    def test_sentiment_field_present_when_scores_exist(self):
        embed = self._embed(
            sentiment=[{"label": "joy", "score": 0.8}, {"label": "sadness", "score": 0.2}]
        )
        field = _get_field(embed, "Sentiment")
        self.assertIsNotNone(field, "Expected a 'Sentiment' field")

    def test_top_emotion_marked_with_arrow(self):
        embed = self._embed(
            sentiment=[{"label": "joy", "score": 0.8}, {"label": "sadness", "score": 0.2}]
        )
        field = _get_field(embed, "Sentiment")
        self.assertIsNotNone(field)
        self.assertIn("◀", field["value"])
        # The arrow must appear on the joy line (top emotion)
        joy_line = next(
            (line for line in field["value"].splitlines() if "joy" in line), None
        )
        self.assertIsNotNone(joy_line, "No 'joy' line found in Sentiment field")
        self.assertIn("◀", joy_line)

    # -- fire pulses field --

    def test_fire_pulses_field_present(self):
        embed = self._embed()
        field = _get_field(embed, "Fire Pulses")
        self.assertIsNotNone(field, "Expected a 'Fire Pulses' field")

    # -- footer --

    def test_footer_says_passed(self):
        embed = self._embed(passed=True)
        self.assertIsNotNone(embed.footer)
        self.assertIn("PASSED", embed.footer)

    def test_footer_says_failed(self):
        embed = self._embed(passed=False)
        self.assertIsNotNone(embed.footer)
        self.assertIn("FAILED", embed.footer)


if __name__ == "__main__":
    unittest.main()

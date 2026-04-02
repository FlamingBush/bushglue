"""
Tests for stt_t2v.py — query functions, sentiment firing logic, and broker
discovery. No live services, hardware, or MQTT broker required.

Run from bushglue/:
    python -m pytest tests/test_stt_t2v.py -v
"""
import json
import os
import sys
import unittest
from unittest.mock import MagicMock, patch

_BUSHGLUE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _BUSHGLUE)

import stt_t2v  # noqa: E402


def _mock_urlopen(data: dict):
    """Return a context-manager mock that yields the given dict as JSON."""
    resp = MagicMock()
    resp.read.return_value = json.dumps(data).encode()
    resp.__enter__ = lambda s: s
    resp.__exit__ = MagicMock(return_value=False)
    return resp


# ── query_t2v ────────────────────────────────────────────────────────────────

class TestQueryT2V(unittest.TestCase):

    def test_returns_verse_dict(self):
        payload = {"text": "For God so loved the world", "id": "JHN-3-16"}
        with patch("urllib.request.urlopen", return_value=_mock_urlopen(payload)):
            result = stt_t2v.query_t2v("love")
        self.assertEqual(result["text"], "For God so loved the world")

    def test_sends_question_in_request_body(self):
        captured = {}
        def fake_urlopen(req, timeout=None):
            captured["body"] = json.loads(req.data)
            return _mock_urlopen({"text": "verse", "id": "x"})
        with patch("urllib.request.urlopen", side_effect=fake_urlopen):
            stt_t2v.query_t2v("wilderness fire")
        self.assertEqual(captured["body"]["question"], "wilderness fire")

    def test_returns_all_fields_from_server(self):
        payload = {"text": "a verse", "id": "PSA-23-1", "score": 0.92}
        with patch("urllib.request.urlopen", return_value=_mock_urlopen(payload)):
            result = stt_t2v.query_t2v("shepherd")
        self.assertIn("score", result)
        self.assertIn("id", result)


# ── query_sentiment ──────────────────────────────────────────────────────────

class TestQuerySentiment(unittest.TestCase):

    def test_returns_classification_list(self):
        scores = [{"label": "joy", "score": 0.9}, {"label": "love", "score": 0.1}]
        with patch("urllib.request.urlopen", return_value=_mock_urlopen({"classification": scores})):
            result = stt_t2v.query_sentiment("I feel great")
        self.assertEqual(result, scores)

    def test_missing_key_returns_empty_list(self):
        with patch("urllib.request.urlopen", return_value=_mock_urlopen({})):
            result = stt_t2v.query_sentiment("hmm")
        self.assertEqual(result, [])

    def test_sends_text_in_request_body(self):
        captured = {}
        def fake_urlopen(req, timeout=None):
            captured["body"] = json.loads(req.data)
            return _mock_urlopen({"classification": []})
        with patch("urllib.request.urlopen", side_effect=fake_urlopen):
            stt_t2v.query_sentiment("burning bush")
        self.assertEqual(captured["body"]["text"], "burning bush")


# ── format_sentiment ─────────────────────────────────────────────────────────

class TestFormatSentiment(unittest.TestCase):

    def test_top_3_in_descending_order(self):
        scores = [
            {"label": "joy",     "score": 0.7},
            {"label": "sadness", "score": 0.1},
            {"label": "anger",   "score": 0.5},
            {"label": "fear",    "score": 0.05},
        ]
        result = stt_t2v.format_sentiment(scores)
        labels = [part.split(":")[0].strip() for part in result.split("  ")]
        self.assertEqual(labels, ["joy", "anger", "sadness"])

    def test_single_score(self):
        scores = [{"label": "love", "score": 0.99}]
        result = stt_t2v.format_sentiment(scores)
        self.assertIn("love", result)
        self.assertIn("0.99", result)

    def test_empty_returns_empty_string(self):
        self.assertEqual(stt_t2v.format_sentiment([]), "")


# ── fire_mqtt ────────────────────────────────────────────────────────────────

class TestFireMqtt(unittest.TestCase):

    def _client(self):
        return MagicMock()

    def test_empty_scores_publishes_nothing(self):
        client = self._client()
        stt_t2v.fire_mqtt([], client)
        client.publish.assert_not_called()

    def test_unknown_emotion_publishes_nothing(self):
        client = self._client()
        stt_t2v.fire_mqtt([{"label": "neutral", "score": 1.0}], client)
        client.publish.assert_not_called()

    def test_joy_publishes_flare_not_bigjet(self):
        # EMOTION_MAP joy: bigjet=0, so only flare fires
        client = self._client()
        stt_t2v.fire_mqtt([{"label": "joy", "score": 1.0}], client)
        topics = [c.args[0] for c in client.publish.call_args_list]
        self.assertIn(stt_t2v.TOPIC_FLARE, topics)
        self.assertNotIn(stt_t2v.TOPIC_BIGJET, topics)

    def test_anger_publishes_flare_and_bigjet(self):
        client = self._client()
        stt_t2v.fire_mqtt([{"label": "anger", "score": 1.0}], client)
        topics = [c.args[0] for c in client.publish.call_args_list]
        self.assertIn(stt_t2v.TOPIC_FLARE, topics)
        self.assertIn(stt_t2v.TOPIC_BIGJET, topics)

    def test_flare_duration_scaled_by_score(self):
        # duration = int(mapping["flare"] * score)
        client = self._client()
        stt_t2v.fire_mqtt([{"label": "surprise", "score": 0.5}], client)
        flare_calls = [c for c in client.publish.call_args_list
                       if c.args[0] == stt_t2v.TOPIC_FLARE]
        expected = int(stt_t2v.EMOTION_MAP["surprise"]["flare"] * 0.5)
        self.assertEqual(flare_calls[0].args[1], expected)

    def test_picks_highest_scoring_emotion(self):
        # sadness scores higher than joy here
        client = self._client()
        stt_t2v.fire_mqtt([
            {"label": "joy",     "score": 0.3},
            {"label": "sadness", "score": 0.7},
        ], client)
        # sadness maps to flare=200, bigjet=0
        flare_calls = [c for c in client.publish.call_args_list
                       if c.args[0] == stt_t2v.TOPIC_FLARE]
        expected = int(stt_t2v.EMOTION_MAP["sadness"]["flare"] * 0.7)
        self.assertEqual(flare_calls[0].args[1], expected)

    def test_zero_score_publishes_nothing(self):
        # int(flare * 0.0) == 0, which is falsy — nothing published
        client = self._client()
        stt_t2v.fire_mqtt([{"label": "joy", "score": 0.0}], client)
        client.publish.assert_not_called()


# ── _windows_host_ip (broker discovery) ──────────────────────────────────────

class TestWindowsHostIp(unittest.TestCase):

    def test_extracts_gateway_from_default_route(self):
        fake_output = (
            "default via 192.168.1.1 dev eth0 proto dhcp\n"
            "192.168.1.0/24 dev eth0 proto kernel\n"
        )
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(stdout=fake_output, returncode=0)
            ip = stt_t2v._windows_host_ip()
        self.assertEqual(ip, "192.168.1.1")

    def test_returns_localhost_when_no_default_route(self):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(stdout="no routes here\n", returncode=0)
            ip = stt_t2v._windows_host_ip()
        self.assertEqual(ip, "localhost")

    def test_returns_localhost_on_exception(self):
        with patch("subprocess.run", side_effect=OSError("no ip binary")):
            ip = stt_t2v._windows_host_ip()
        self.assertEqual(ip, "localhost")


if __name__ == "__main__":
    unittest.main()

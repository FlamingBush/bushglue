"""Unit tests for the bush-ptt indicator LED.

The LED tracks bush/pipeline/stt/listening, not the switch contacts, so the
light means "you are being heard" rather than "the button is down".
"""
import importlib
import json
import sys
from unittest.mock import MagicMock

import pytest


def _reload_bush_ptt(monkeypatch, **env):
    """Reload bush_ptt so module-level env-derived constants pick up changes."""
    for k, v in env.items():
        if v is None:
            monkeypatch.delenv(k, raising=False)
        else:
            monkeypatch.setenv(k, v)
    if "bush_ptt" in sys.modules:
        del sys.modules["bush_ptt"]
    return importlib.import_module("bush_ptt")


class FakeMsg:
    def __init__(self, topic, payload):
        self.topic = topic
        self.payload = payload if isinstance(payload, bytes) else payload.encode()


@pytest.fixture
def ptt(monkeypatch):
    mod = _reload_bush_ptt(monkeypatch)
    led = MagicMock()
    mod._led[0] = led
    yield mod, led
    mod._led[0] = None


def listening(state):
    return FakeMsg("bush/pipeline/stt/listening", json.dumps({"listening": state}))


# ── LED follows listening state ─────────────────────────────────────────────

def test_listening_true_lights_the_led(ptt):
    mod, led = ptt
    mod.on_message(None, None, listening(True))
    led.write.assert_called_once_with(True)


def test_listening_false_darkens_the_led(ptt):
    mod, led = ptt
    mod.on_message(None, None, listening(False))
    led.write.assert_called_once_with(False)


def test_other_topics_are_ignored(ptt):
    mod, led = ptt
    mod.on_message(None, None, FakeMsg("bush/pipeline/stt/ptt",
                                       json.dumps({"pressed": True})))
    led.write.assert_not_called()


def test_malformed_payload_does_not_raise(ptt):
    mod, led = ptt
    mod.on_message(None, None, FakeMsg("bush/pipeline/stt/listening", b"not json"))
    led.write.assert_not_called()


def test_missing_key_reads_as_not_listening(ptt):
    mod, led = ptt
    mod.on_message(None, None, FakeMsg("bush/pipeline/stt/listening", b"{}"))
    led.write.assert_called_once_with(False)


# ── active-low wiring (transistor low-side driver) ──────────────────────────

def test_active_low_inverts_the_level(monkeypatch):
    mod = _reload_bush_ptt(monkeypatch, PTT_LED_ACTIVE_LOW="1")
    led = MagicMock()
    mod._led[0] = led
    try:
        mod.on_message(None, None, listening(True))
        led.write.assert_called_once_with(False)
    finally:
        mod._led[0] = None


# ── degraded / absent hardware ──────────────────────────────────────────────

def test_no_led_configured_is_a_noop(monkeypatch):
    mod = _reload_bush_ptt(monkeypatch, PTT_LED_LINE="")
    assert mod._open_led() is None
    mod._set_led(True)          # must not raise with no LED present


def test_led_write_failure_does_not_propagate(ptt):
    mod, led = ptt
    led.write.side_effect = OSError("gpio gone")
    mod.on_message(None, None, listening(True))   # swallowed, button lives on


def test_shutdown_darkens_and_releases(ptt):
    mod, led = ptt
    mod._shutdown()
    led.write.assert_called_once_with(False)
    led.close.assert_called_once()
    assert mod._led[0] is None

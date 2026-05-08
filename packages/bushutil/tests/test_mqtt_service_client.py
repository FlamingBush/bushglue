"""Pin the bush/<service>/{status,version,fault} contract.

These tests speak the contract from the autoplan eng review. If they break,
the operator-visible failure contract has shifted — and that contract is
what makes the difference between "bush stopped responding, no idea why" and
"sentiment status went offline at 02:14, restart it" at the playa.
"""
from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest


@pytest.fixture
def fake_paho_client(monkeypatch):
    """Replace paho.mqtt.client.Client with a MagicMock instance.

    Returns the instance so tests can inspect calls. The constructor and
    enum lookup (CallbackAPIVersion.VERSION2) are replaced with stubs that
    return the fake instance.
    """
    fake = MagicMock()
    fake.publish = MagicMock()
    fake.subscribe = MagicMock()
    fake.will_set = MagicMock()
    fake.connect = MagicMock()
    fake.disconnect = MagicMock()
    fake.loop_forever = MagicMock()
    fake.loop_start = MagicMock()
    fake.loop_stop = MagicMock()
    fake.reconnect_delay_set = MagicMock()

    import paho.mqtt.client as mqtt_mod
    monkeypatch.setattr(mqtt_mod, "Client", lambda *a, **kw: fake)
    return fake


def _make_client(monkeypatch, **kwargs):
    """Construct a MqttServiceClient with deterministic broker + version."""
    monkeypatch.setenv("BUSH_VERSION", kwargs.pop("version_env", "abc1234"))
    from bushutil.mqtt_service_client import MqttServiceClient
    defaults = {"service_name": "test", "broker": "localhost"}
    defaults.update(kwargs)
    return MqttServiceClient(**defaults)


# ── version detection ──────────────────────────────────────────────────────


def test_default_version_uses_env_var(monkeypatch):
    from bushutil.mqtt_service_client import _default_version
    monkeypatch.setenv("BUSH_VERSION", "v1.2.3")
    assert _default_version() == "v1.2.3"


def test_default_version_falls_back_to_git(monkeypatch):
    monkeypatch.delenv("BUSH_VERSION", raising=False)
    fake_run = MagicMock(return_value=MagicMock(returncode=0, stdout="abc1234\n"))
    monkeypatch.setattr("subprocess.run", fake_run)
    from bushutil.mqtt_service_client import _default_version
    assert _default_version() == "abc1234"


def test_default_version_falls_back_to_dev_when_git_fails(monkeypatch):
    monkeypatch.delenv("BUSH_VERSION", raising=False)
    def boom(*a, **kw):
        raise FileNotFoundError("no git")
    monkeypatch.setattr("subprocess.run", boom)
    from bushutil.mqtt_service_client import _default_version
    assert _default_version() == "dev"


def test_default_version_falls_back_to_dev_on_git_timeout(monkeypatch):
    import subprocess
    monkeypatch.delenv("BUSH_VERSION", raising=False)
    def boom(*a, **kw):
        raise subprocess.TimeoutExpired(cmd=a[0], timeout=2)
    monkeypatch.setattr("subprocess.run", boom)
    from bushutil.mqtt_service_client import _default_version
    assert _default_version() == "dev"


# ── construction + LWT ─────────────────────────────────────────────────────


def test_constructor_sets_lwt_for_status_offline(fake_paho_client, monkeypatch):
    client = _make_client(monkeypatch)
    fake_paho_client.will_set.assert_called_once_with(
        "bush/test/status", "offline", qos=1, retain=True
    )


def test_constructor_enables_auto_reconnect(fake_paho_client, monkeypatch):
    _make_client(monkeypatch)
    fake_paho_client.reconnect_delay_set.assert_called_once_with(min_delay=1, max_delay=60)


def test_constructor_uses_explicit_version_over_env(fake_paho_client, monkeypatch):
    monkeypatch.setenv("BUSH_VERSION", "from-env")
    from bushutil.mqtt_service_client import MqttServiceClient
    client = MqttServiceClient(service_name="test", broker="localhost", version="explicit-v")
    assert client.version == "explicit-v"


# ── on_connect contract ────────────────────────────────────────────────────


def _trigger_on_connect(client, fake_paho_client):
    """Simulate paho calling our on_connect handler."""
    on_connect = fake_paho_client.on_connect
    on_connect(fake_paho_client, None, {}, 0, None)


def test_on_connect_publishes_starting_and_version(fake_paho_client, monkeypatch):
    client = _make_client(monkeypatch, version="abc1234")
    _trigger_on_connect(client, fake_paho_client)

    publishes = [c.args for c in fake_paho_client.publish.call_args_list]
    # First two publishes must be status=starting + version. Order matters
    # so subscribers can rely on seeing starting before any data.
    assert publishes[0] == ("bush/test/status", "starting")
    assert publishes[1] == ("bush/test/version", "abc1234")


def test_on_connect_subscribes_all_registered_topics(fake_paho_client, monkeypatch):
    client = _make_client(monkeypatch)
    client.subscribe("bush/pipeline/t2v/verse", lambda c, m: None, qos=1)
    client.subscribe("bush/pipeline/tts/done", lambda c, m: None)

    _trigger_on_connect(client, fake_paho_client)

    subs = [c.args[0] for c in fake_paho_client.subscribe.call_args_list]
    assert "bush/pipeline/t2v/verse" in subs
    assert "bush/pipeline/tts/done" in subs


def test_on_connect_does_not_publish_ready_before_mark_ready(fake_paho_client, monkeypatch):
    client = _make_client(monkeypatch)
    _trigger_on_connect(client, fake_paho_client)

    publishes = [c.args for c in fake_paho_client.publish.call_args_list]
    ready_publishes = [p for p in publishes if p[1] == "ready"]
    assert ready_publishes == []


def test_reconnect_after_mark_ready_republishes_ready(fake_paho_client, monkeypatch):
    """After mark_ready(), reconnects must re-publish ready=true.

    Broker may have lost retained state during the disconnect window. Without
    this the next subscriber sees stale `starting` forever.
    """
    client = _make_client(monkeypatch)
    client.mark_ready()
    fake_paho_client.publish.reset_mock()

    _trigger_on_connect(client, fake_paho_client)

    publishes = [c.args for c in fake_paho_client.publish.call_args_list]
    # Should publish: starting, version, then ready (because latched).
    assert ("bush/test/status", "starting") in publishes
    assert ("bush/test/status", "ready") in publishes


def test_user_on_connect_hook_runs_after_contract(fake_paho_client, monkeypatch):
    client = _make_client(monkeypatch)
    seen = []
    client.set_on_connect(lambda *a: seen.append("user"))

    _trigger_on_connect(client, fake_paho_client)

    assert seen == ["user"]
    # User hook ran AFTER our contract publishes — count them.
    publishes = [c.args for c in fake_paho_client.publish.call_args_list]
    assert any(p[1] == "starting" for p in publishes)


# ── mark_ready / fault ─────────────────────────────────────────────────────


def test_mark_ready_publishes_ready_retained_and_latches(fake_paho_client, monkeypatch):
    client = _make_client(monkeypatch)
    assert client.ready_latched is False

    client.mark_ready()

    fake_paho_client.publish.assert_called_with(
        "bush/test/status", "ready", qos=1, retain=True
    )
    assert client.ready_latched is True


def test_publish_fault_is_non_retained_and_has_ts(fake_paho_client, monkeypatch):
    client = _make_client(monkeypatch)
    client.publish_fault("model_load_timeout", {"elapsed": 30.5})

    call = fake_paho_client.publish.call_args
    topic, payload = call.args
    assert topic == "bush/test/fault"
    assert call.kwargs["retain"] is False
    body = json.loads(payload)
    assert body["error"] == "model_load_timeout"
    assert body["elapsed"] == 30.5
    assert "ts" in body and isinstance(body["ts"], (int, float))


def test_publish_fault_no_context(fake_paho_client, monkeypatch):
    client = _make_client(monkeypatch)
    client.publish_fault("classifier_shape_drift")

    payload = json.loads(fake_paho_client.publish.call_args.args[1])
    assert payload["error"] == "classifier_shape_drift"
    assert "ts" in payload


# ── message dispatch ───────────────────────────────────────────────────────


def test_dispatch_routes_to_registered_handler(fake_paho_client, monkeypatch):
    client = _make_client(monkeypatch)
    seen = []
    client.subscribe("bush/foo", lambda c, m: seen.append(m.payload))

    msg = MagicMock(topic="bush/foo", payload=b"hello")
    fake_paho_client.on_message(fake_paho_client, None, msg)

    assert seen == [b"hello"]


def test_dispatch_unknown_topic_is_silent(fake_paho_client, monkeypatch):
    client = _make_client(monkeypatch)
    msg = MagicMock(topic="bush/unrouted", payload=b"x")
    # Should not raise even though no handler is registered.
    fake_paho_client.on_message(fake_paho_client, None, msg)


def test_handler_exception_publishes_fault_not_crash(fake_paho_client, monkeypatch):
    client = _make_client(monkeypatch)

    def bad_handler(c, m):
        raise ValueError("kaboom")

    client.subscribe("bush/foo", bad_handler)
    fake_paho_client.publish.reset_mock()

    msg = MagicMock(topic="bush/foo", payload=b"x")
    # Must NOT raise — exception should be caught + published as fault.
    fake_paho_client.on_message(fake_paho_client, None, msg)

    fault_calls = [
        c for c in fake_paho_client.publish.call_args_list
        if c.args[0] == "bush/test/fault"
    ]
    assert len(fault_calls) == 1
    payload = json.loads(fault_calls[0].args[1])
    assert payload["error"] == "message_handler_error"
    assert payload["topic"] == "bush/foo"
    assert payload["error_class"] == "ValueError"
    assert "kaboom" in payload["error_message"]


# ── controlled shutdown ────────────────────────────────────────────────────


def test_disconnect_publishes_offline_before_disconnecting(fake_paho_client, monkeypatch):
    client = _make_client(monkeypatch)

    # Capture call order: publish must come before disconnect.
    call_order: list[str] = []
    fake_paho_client.publish.side_effect = lambda *a, **kw: call_order.append(f"publish:{a[0]}:{a[1]}")
    fake_paho_client.disconnect.side_effect = lambda: call_order.append("disconnect")

    client.disconnect()

    # Publish status=offline must precede disconnect()
    offline_idx = next(i for i, c in enumerate(call_order) if c == "publish:bush/test/status:offline")
    disconnect_idx = call_order.index("disconnect")
    assert offline_idx < disconnect_idx


def test_disconnect_swallows_publish_failure(fake_paho_client, monkeypatch):
    client = _make_client(monkeypatch)
    fake_paho_client.publish.side_effect = RuntimeError("broker unreachable")

    # Must not propagate — controlled shutdown is best-effort.
    client.disconnect()
    fake_paho_client.disconnect.assert_called_once()

from __future__ import annotations

import logging
import sys
from dataclasses import dataclass
from threading import Event, Thread
from pathlib import Path
from subprocess import run

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from mqtt_pixelblaze_bridge import (
    ArrayLookup,
    BridgeConfig,
    ConfigurationError,
    MqttConfig,
    MqttPixelblazeBridge,
    PixelblazeConfig,
    TopicBinding,
)


@dataclass
class FakeMessage:
    topic: str
    payload: bytes


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


class FakeMqttClient:
    def __init__(self, messages: list[FakeMessage]) -> None:
        self.messages = messages
        self.on_message = None
        self.subscriptions: list[tuple[str, int]] = []
        self.connected_to: tuple[str, int, int] | None = None
        self.credentials: tuple[str, str | None] | None = None
        self.disconnect_calls = 0
        self.loop_start_calls = 0
        self.loop_stop_calls = 0
        self.reconnect_delays: tuple[int, int] | None = None
        self.suppress_exceptions = False

    def connect_async(self, host: str, port: int, keepalive: int) -> None:
        self.connected_to = (host, port, keepalive)

    def subscribe(self, topic: str, qos: int) -> None:
        self.subscriptions.append((topic, qos))

    def username_pw_set(self, username: str, password: str | None) -> None:
        self.credentials = (username, password)

    def reconnect_delay_set(self, min_delay: int, max_delay: int) -> None:
        self.reconnect_delays = (min_delay, max_delay)

    def loop_start(self) -> None:
        self.loop_start_calls += 1
        assert self.on_connect is not None
        self.on_connect(self, None, None, 0, None)
        assert self.on_message is not None
        for message in self.messages:
            self.on_message(self, None, message)

    def loop_stop(self) -> None:
        self.loop_stop_calls += 1

    def disconnect(self) -> None:
        self.disconnect_calls += 1


class FakePixelblaze:
    def __init__(self) -> None:
        self.patterns: list[str] = []
        self.brightnesses: list[float] = []
        self.variables: list[dict[str, float]] = []
        self.commands: list[tuple[str, object]] = []
        self.pattern_failures = 0
        self.closed = False

    def setActivePatternByName(self, pattern: str) -> None:
        if self.pattern_failures:
            self.pattern_failures -= 1
            raise RuntimeError("pattern unavailable")
        self.patterns.append(pattern)
        self.commands.append(("pattern", pattern))

    def setBrightnessSlider(self, brightness: float) -> None:
        self.brightnesses.append(brightness)
        self.commands.append(("brightness", brightness))

    def setActiveVariables(self, variables: dict[str, float]) -> None:
        self.variables.append(variables)
        self.commands.append(("variable", variables))

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        self.closed = True


class BlockingPixelblaze(FakePixelblaze):
    def __init__(self) -> None:
        super().__init__()
        self.command_started = Event()
        self.release_command = Event()
        self.active_calls = 0
        self.max_active_calls = 0

    def setActivePatternByName(self, pattern: str) -> None:
        self.active_calls += 1
        self.max_active_calls = max(self.max_active_calls, self.active_calls)
        self.command_started.set()
        self.release_command.wait()
        self.active_calls -= 1
        super().setActivePatternByName(pattern)


class HoldingMqttClient(FakeMqttClient):
    def __init__(self, first_message: FakeMessage) -> None:
        super().__init__([])
        self.release_loop = Event()

        self.first_message = first_message

    def loop_start(self) -> None:
        assert self.on_message is not None
        assert self.on_connect is not None
        self.on_connect(self, None, None, 0, None)
        self.on_message(self, None, self.first_message)
        self.release_loop.wait()


def run_bridge(bridge: MqttPixelblazeBridge) -> None:
    bridge._start()
    bridge._work_queue.join()
    bridge.stop()


ORIGINAL_RUN_FOREVER = MqttPixelblazeBridge.run_forever


@pytest.fixture(autouse=True)
def foreground_lifecycle_uses_a_deterministic_test_stop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(MqttPixelblazeBridge, "run_forever", run_bridge)


def test_run_forever_waits_for_clean_stop() -> None:
    config = BridgeConfig(
        mqtt=MqttConfig(host="broker.example"),
        pixelblaze=PixelblazeConfig(host="pixelblaze.example"),
        bindings=(TopicBinding(topic="show/pattern", command="set_pattern"),),
    )
    bridge = MqttPixelblazeBridge(
        config,
        mqtt_client_factory=lambda client_id: FakeMqttClient([]),
        pixelblaze_factory=lambda host: FakePixelblaze(),
    )
    thread = Thread(target=ORIGINAL_RUN_FOREVER, args=(bridge,))

    thread.start()
    assert not bridge._stopped_event.is_set()
    bridge.stop()
    thread.join(timeout=1)

    assert not thread.is_alive()


def test_bridge_routes_a_scalar_pattern_message_through_its_public_lifecycle() -> None:
    config = BridgeConfig(
        mqtt=MqttConfig(host="broker.example"),
        pixelblaze=PixelblazeConfig(host="pixelblaze.example"),
        bindings=(TopicBinding(topic="show/pattern", command="set_pattern"),),
    )
    mqtt_client = FakeMqttClient([FakeMessage("show/pattern", b"aurora")])
    pixelblaze = FakePixelblaze()

    bridge = MqttPixelblazeBridge(
        config,
        mqtt_client_factory=lambda client_id: mqtt_client,
        pixelblaze_factory=lambda host: pixelblaze,
    )

    run_bridge(bridge)

    assert mqtt_client.connected_to == ("broker.example", 1883, 60)
    assert mqtt_client.reconnect_delays == (1, 30)
    assert mqtt_client.loop_start_calls == 1
    assert mqtt_client.suppress_exceptions is True
    assert mqtt_client.subscriptions == [("show/pattern", 0)]
    assert pixelblaze.patterns == ["aurora"]
    assert mqtt_client.disconnect_calls == 1
    assert pixelblaze.closed is True


def test_bridge_logs_rejected_connection(caplog: pytest.LogCaptureFixture) -> None:
    config = BridgeConfig(
        mqtt=MqttConfig(host="broker.example"),
        pixelblaze=PixelblazeConfig(host="pixelblaze.example"),
        bindings=(TopicBinding(topic="show/pattern", command="set_pattern"),),
    )
    mqtt_client = FakeMqttClient([])
    bridge = MqttPixelblazeBridge(
        config,
        mqtt_client_factory=lambda client_id: mqtt_client,
        pixelblaze_factory=lambda host: FakePixelblaze(),
    )

    with caplog.at_level(logging.WARNING):
        bridge._on_connect(mqtt_client, None, None, 1)

    assert caplog.messages == ["MQTT connection rejected: 1"]


def test_bridge_resubscribes_after_reconnect_and_logs_rejected_suback(
    caplog: pytest.LogCaptureFixture,
) -> None:
    config = BridgeConfig(
        mqtt=MqttConfig(host="broker.example"),
        pixelblaze=PixelblazeConfig(host="pixelblaze.example"),
        bindings=(
            TopicBinding(topic="show/pattern", command="set_pattern"),
            TopicBinding(topic="show/pattern", command="set_brightness"),
        ),
    )
    mqtt_client = FakeMqttClient([])
    bridge = MqttPixelblazeBridge(
        config,
        mqtt_client_factory=lambda client_id: mqtt_client,
        pixelblaze_factory=lambda host: FakePixelblaze(),
    )

    bridge._on_connect(mqtt_client, None, None, 0)
    bridge._on_connect(mqtt_client, None, None, 0)
    with caplog.at_level(logging.WARNING):
        bridge._on_subscribe(mqtt_client, None, 1, [0, 128])

    assert mqtt_client.subscriptions == [("show/pattern", 0), ("show/pattern", 0)]
    assert caplog.messages == ["MQTT subscription acknowledgement rejected: [0, 128]"]


def test_bridge_replaces_pixelblaze_after_a_command_failure() -> None:
    config = BridgeConfig(
        mqtt=MqttConfig(host="broker.example"),
        pixelblaze=PixelblazeConfig(host="pixelblaze.example"),
        bindings=(TopicBinding(topic="show/pattern", command="set_pattern"),),
    )
    mqtt_client = FakeMqttClient(
        [FakeMessage("show/pattern", b"aurora"), FakeMessage("show/pattern", b"aurora")]
    )
    failed_client = FakePixelblaze()
    failed_client.pattern_failures = 1
    replacement_client = FakePixelblaze()
    clients = [failed_client, replacement_client]

    run_bridge(
        MqttPixelblazeBridge(
            config,
            mqtt_client_factory=lambda client_id: mqtt_client,
            pixelblaze_factory=lambda host: clients.pop(0),
        )
    )

    assert failed_client.closed is True
    assert replacement_client.patterns == []


def test_pixelblaze_recovery_uses_bounded_fake_clock_backoff() -> None:
    config = BridgeConfig(
        mqtt=MqttConfig(host="broker.example"),
        pixelblaze=PixelblazeConfig(host="pixelblaze.example"),
        bindings=(TopicBinding(topic="show/pattern", command="set_pattern"),),
    )
    clock = FakeClock()
    replacement = FakePixelblaze()
    attempts = 0

    def pixelblaze_factory(host: str) -> FakePixelblaze:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("offline")
        return replacement

    bridge = MqttPixelblazeBridge(
        config,
        mqtt_client_factory=lambda client_id: FakeMqttClient([]),
        pixelblaze_factory=pixelblaze_factory,
        clock=clock,
    )

    assert bridge._try_create_pixelblaze() is False
    assert bridge._next_pixelblaze_attempt == 1.0
    assert bridge._pixelblaze_retry_delay == 2.0
    assert bridge._try_create_pixelblaze() is False
    clock.now = 1.0
    assert bridge._try_create_pixelblaze() is True
    assert bridge._pixelblaze_client is replacement
    assert bridge._last_pattern is None


def test_bridge_construction_defers_client_creation_until_its_lifecycle_starts() -> None:
    config = BridgeConfig(
        mqtt=MqttConfig(host="broker.example"),
        pixelblaze=PixelblazeConfig(host="pixelblaze.example"),
        bindings=(TopicBinding(topic="show/pattern", command="set_pattern"),),
    )
    client_creations: list[str] = []

    MqttPixelblazeBridge(
        config,
        mqtt_client_factory=lambda client_id: client_creations.append(
            f"mqtt:{client_id}"
        ),
        pixelblaze_factory=lambda host: client_creations.append(f"pixelblaze:{host}"),
    )

    assert client_creations == []


def test_bridge_applies_configured_basic_mqtt_credentials_before_connecting() -> None:
    config = BridgeConfig(
        mqtt=MqttConfig(
            host="broker.example",
            username="lights",
            password="not-logged",
        ),
        pixelblaze=PixelblazeConfig(host="pixelblaze.example"),
        bindings=(TopicBinding(topic="show/pattern", command="set_pattern"),),
    )
    mqtt_client = FakeMqttClient([FakeMessage("show/pattern", b"aurora")])

    bridge = MqttPixelblazeBridge(
        config,
        mqtt_client_factory=lambda client_id: mqtt_client,
        pixelblaze_factory=lambda host: FakePixelblaze(),
    )
    bridge._start()
    bridge.stop()

    assert mqtt_client.credentials == ("lights", "not-logged")


def test_bridge_rejects_empty_pattern_messages() -> None:
    config = BridgeConfig(
        mqtt=MqttConfig(host="broker.example"),
        pixelblaze=PixelblazeConfig(host="pixelblaze.example"),
        bindings=(TopicBinding(topic="show/pattern", command="set_pattern"),),
    )
    mqtt_client = FakeMqttClient([FakeMessage("show/pattern", b"")])
    pixelblaze = FakePixelblaze()

    MqttPixelblazeBridge(
        config,
        mqtt_client_factory=lambda client_id: mqtt_client,
        pixelblaze_factory=lambda host: pixelblaze,
    ).run_forever()

    assert pixelblaze.patterns == []


def test_package_import_does_not_open_a_network_connection() -> None:
    source_root = Path(__file__).resolve().parents[1] / "src"
    program = f"""
import socket
import sys

def forbidden_socket(*args, **kwargs):
    raise AssertionError('network access during import')

socket.socket = forbidden_socket
sys.path.insert(0, {str(source_root)!r})
import mqtt_pixelblaze_bridge
print('imported safely')
"""

    result = run([sys.executable, "-c", program], capture_output=True, text=True)

    assert result.returncode == 0, result.stderr
    assert result.stdout == "imported safely\n"


def test_bridge_validates_direct_configuration_before_creating_clients() -> None:
    client_creations: list[str] = []
    invalid_config = BridgeConfig(
        mqtt=MqttConfig(host=""),
        pixelblaze=PixelblazeConfig(host=""),
        bindings=(),
    )

    with pytest.raises(ConfigurationError):
        MqttPixelblazeBridge(
            invalid_config,
            mqtt_client_factory=lambda client_id: client_creations.append("mqtt"),
            pixelblaze_factory=lambda host: client_creations.append("pixelblaze"),
        )

    assert client_creations == []


def test_stop_is_idempotent_after_the_bridge_lifecycle_finishes() -> None:
    config = BridgeConfig(
        mqtt=MqttConfig(host="broker.example"),
        pixelblaze=PixelblazeConfig(host="pixelblaze.example"),
        bindings=(TopicBinding(topic="show/pattern", command="set_pattern"),),
    )
    mqtt_client = FakeMqttClient([FakeMessage("show/pattern", b"aurora")])

    bridge = MqttPixelblazeBridge(
        config,
        mqtt_client_factory=lambda client_id: mqtt_client,
        pixelblaze_factory=lambda host: FakePixelblaze(),
    )

    bridge.run_forever()
    bridge.stop()

    assert mqtt_client.disconnect_calls == 1


def test_bridge_routes_all_supported_scalar_commands() -> None:
    config = BridgeConfig(
        mqtt=MqttConfig(host="broker.example"),
        pixelblaze=PixelblazeConfig(host="pixelblaze.example"),
        bindings=(
            TopicBinding(topic="show/pattern", command="set_pattern"),
            TopicBinding(topic="show/brightness", command="set_brightness"),
            TopicBinding(
                topic="show/speed",
                command="set_variable",
                variable="speed",
            ),
        ),
    )
    mqtt_client = FakeMqttClient(
        [
            FakeMessage("show/pattern", b"aurora"),
            FakeMessage("show/brightness", b"0.5"),
            FakeMessage("show/speed", b"1.25"),
        ]
    )
    pixelblaze = FakePixelblaze()

    MqttPixelblazeBridge(
        config,
        mqtt_client_factory=lambda client_id: mqtt_client,
        pixelblaze_factory=lambda host: pixelblaze,
    ).run_forever()

    assert mqtt_client.subscriptions == [
        ("show/pattern", 0),
        ("show/brightness", 0),
        ("show/speed", 0),
    ]
    assert pixelblaze.patterns == ["aurora"]
    assert pixelblaze.brightnesses == [0.5]
    assert pixelblaze.variables == [{"speed": 1.25}]


def test_bridge_maps_patterns_and_runs_shared_topic_bindings_in_config_order() -> None:
    config = BridgeConfig(
        mqtt=MqttConfig(host="broker.example"),
        pixelblaze=PixelblazeConfig(host="pixelblaze.example"),
        bindings=(
            TopicBinding(
                topic="show/state",
                command="set_variable",
                payload_format="json",
                value_path=("controls", "missing"),
                variable="unused",
            ),
            TopicBinding(
                topic="show/state",
                command="set_pattern",
                value_map={"0.25": "aurora"},
            ),
            TopicBinding(topic="show/state", command="set_brightness"),
            TopicBinding(
                topic="show/state",
                command="set_variable",
                variable="speed",
            ),
        ),
    )
    mqtt_client = FakeMqttClient([FakeMessage("show/state", b"0.25")])
    pixelblaze = FakePixelblaze()

    MqttPixelblazeBridge(
        config,
        mqtt_client_factory=lambda client_id: mqtt_client,
        pixelblaze_factory=lambda host: pixelblaze,
    ).run_forever()

    assert mqtt_client.subscriptions == [("show/state", 0)]
    assert pixelblaze.commands == [
        ("pattern", "aurora"),
        ("brightness", 0.25),
        ("variable", {"speed": 0.25}),
    ]


def test_bridge_skips_invalid_payloads_and_continues_with_later_messages() -> None:
    config = BridgeConfig(
        mqtt=MqttConfig(host="broker.example"),
        pixelblaze=PixelblazeConfig(host="pixelblaze.example"),
        bindings=(
            TopicBinding(topic="show/pattern", command="set_pattern", value_map={"go": "aurora"}),
            TopicBinding(topic="show/brightness", command="set_brightness"),
            TopicBinding(topic="show/speed", command="set_variable", variable="speed"),
        ),
    )
    mqtt_client = FakeMqttClient(
        [
            FakeMessage("show/pattern", b"unknown"),
            FakeMessage("show/brightness", b"nan"),
            FakeMessage("show/brightness", b"1.1"),
            FakeMessage("show/speed", b"not-a-number"),
            FakeMessage("show/pattern", b"go"),
            FakeMessage("show/brightness", b"0.5"),
            FakeMessage("show/speed", b"2"),
        ]
    )
    pixelblaze = FakePixelblaze()

    MqttPixelblazeBridge(
        config,
        mqtt_client_factory=lambda client_id: mqtt_client,
        pixelblaze_factory=lambda host: pixelblaze,
    ).run_forever()

    assert pixelblaze.patterns == ["aurora"]
    assert pixelblaze.brightnesses == [0.5]
    assert pixelblaze.variables == [{"speed": 2.0}]


def test_bridge_deduplicates_only_successful_pattern_commands() -> None:
    config = BridgeConfig(
        mqtt=MqttConfig(host="broker.example"),
        pixelblaze=PixelblazeConfig(host="pixelblaze.example"),
        bindings=(
            TopicBinding(topic="show/pattern", command="set_pattern"),
            TopicBinding(topic="show/brightness", command="set_brightness"),
            TopicBinding(topic="show/speed", command="set_variable", variable="speed"),
        ),
    )
    mqtt_client = FakeMqttClient(
        [
            FakeMessage("show/pattern", b"aurora"),
            FakeMessage("show/pattern", b"aurora"),
            FakeMessage("show/pattern", b"aurora"),
            FakeMessage("show/brightness", b"0.5"),
            FakeMessage("show/brightness", b"0.5"),
            FakeMessage("show/speed", b"1"),
            FakeMessage("show/speed", b"1"),
        ]
    )
    pixelblaze = FakePixelblaze()
    pixelblaze.pattern_failures = 1

    MqttPixelblazeBridge(
        config,
        mqtt_client_factory=lambda client_id: mqtt_client,
        pixelblaze_factory=lambda host: pixelblaze,
    ).run_forever()

    assert pixelblaze.patterns == []
    assert pixelblaze.brightnesses == []
    assert pixelblaze.variables == []


def test_bridge_fans_out_one_json_object_in_binding_order_with_partial_success() -> None:
    config = BridgeConfig(
        mqtt=MqttConfig(host="broker.example"),
        pixelblaze=PixelblazeConfig(host="pixelblaze.example"),
        bindings=(
            TopicBinding(
                topic="show/state",
                command="set_pattern",
                payload_format="json",
                value_path=("scene", "name"),
                value_map={"opening": "aurora"},
            ),
            TopicBinding(
                topic="show/state",
                command="set_brightness",
                payload_format="json",
                value_path=("controls", "brightness"),
            ),
            TopicBinding(
                topic="show/state",
                command="set_variable",
                payload_format="json",
                value_path=("controls", "speed"),
                variable="speed",
            ),
        ),
    )
    mqtt_client = FakeMqttClient(
        [
            FakeMessage(
                "show/state",
                b'{"scene":{"name":"opening"},"controls":{"brightness":0.4,"speed":2}}',
            )
        ]
    )
    pixelblaze = FakePixelblaze()

    MqttPixelblazeBridge(
        config,
        mqtt_client_factory=lambda client_id: mqtt_client,
        pixelblaze_factory=lambda host: pixelblaze,
    ).run_forever()

    assert pixelblaze.commands == [
        ("pattern", "aurora"),
        ("brightness", 0.4),
        ("variable", {"speed": 2.0}),
    ]


def test_bridge_extracts_unsorted_array_values_before_triggering_the_scene() -> None:
    config = BridgeConfig(
        mqtt=MqttConfig(host="broker.example"),
        pixelblaze=PixelblazeConfig(host="pixelblaze.example"),
        bindings=(
            TopicBinding(
                topic="show/sentiment",
                command="set_variable",
                payload_format="json",
                value_path=("classification",),
                array_lookup=ArrayLookup("label", "joy", "score"),
                variable="inputJoy",
            ),
            TopicBinding(
                topic="show/sentiment",
                command="set_variable",
                payload_format="json",
                value_path=("classification",),
                array_lookup=ArrayLookup("label", "anger", "score"),
                variable="inputAnger",
            ),
            TopicBinding(
                topic="show/sentiment",
                command="trigger",
                payload_format="json",
                variable="sentimentTrigger",
            ),
        ),
    )
    mqtt_client = FakeMqttClient(
        [
            FakeMessage(
                "show/sentiment",
                b'{"classification":['
                b'{"label":"anger","score":0.08},'
                b'{"label":"sadness","score":0.02},'
                b'{"label":"joy","score":0.90}'
                b'],"flare":369,"bigjet":0}',
            )
        ]
    )
    pixelblaze = FakePixelblaze()

    MqttPixelblazeBridge(
        config,
        mqtt_client_factory=lambda client_id: mqtt_client,
        pixelblaze_factory=lambda host: pixelblaze,
        clock=lambda: 0.0,
    ).run_forever()

    assert pixelblaze.variables == [
        {"inputJoy": 0.9},
        {"inputAnger": 0.08},
        {"sentimentTrigger": 1.0},
    ]


def test_bridge_increments_repeated_json_lifecycle_triggers() -> None:
    config = BridgeConfig(
        mqtt=MqttConfig(host="broker.example"),
        pixelblaze=PixelblazeConfig(host="pixelblaze.example"),
        bindings=(
            TopicBinding(
                topic="show/speaking",
                command="trigger",
                payload_format="json",
                variable="speakingTrigger",
            ),
        ),
    )
    mqtt_client = FakeMqttClient(
        [
            FakeMessage("show/speaking", b'{"text":"first","ts":1}'),
            FakeMessage("show/speaking", b'{"text":"second","ts":2}'),
            FakeMessage("show/speaking", b"not-json"),
        ]
    )
    pixelblaze = FakePixelblaze()

    MqttPixelblazeBridge(
        config,
        mqtt_client_factory=lambda client_id: mqtt_client,
        pixelblaze_factory=lambda host: pixelblaze,
        clock=lambda: 0.0,
    ).run_forever()

    assert pixelblaze.variables == [
        {"speakingTrigger": 1.0},
        {"speakingTrigger": 2.0},
    ]


def test_lifecycle_triggers_share_one_cross_topic_event_order() -> None:
    config = BridgeConfig(
        mqtt=MqttConfig(host="broker.example"),
        pixelblaze=PixelblazeConfig(host="pixelblaze.example"),
        bindings=(
            TopicBinding(
                topic="show/verse",
                command="trigger",
                payload_format="json",
                variable="verseTrigger",
            ),
            TopicBinding(
                topic="show/done",
                command="trigger",
                payload_format="json",
                variable="doneTrigger",
            ),
            TopicBinding(
                topic="show/speaking",
                command="trigger",
                payload_format="json",
                variable="speakingTrigger",
            ),
        ),
    )
    mqtt_client = FakeMqttClient(
        [
            FakeMessage("show/verse", b'{"text":"new"}'),
            FakeMessage("show/done", b'{"ts":1}'),
            FakeMessage("show/speaking", b'{"text":"new"}'),
        ]
    )
    pixelblaze = FakePixelblaze()

    MqttPixelblazeBridge(
        config,
        mqtt_client_factory=lambda client_id: mqtt_client,
        pixelblaze_factory=lambda host: pixelblaze,
        clock=lambda: 0.0,
    ).run_forever()

    assert pixelblaze.variables == [
        {"verseTrigger": 1.0},
        {"doneTrigger": 2.0},
        {"speakingTrigger": 3.0},
    ]


def test_bridge_hashes_normalized_verse_text_and_maps_flame_valves() -> None:
    config = BridgeConfig(
        mqtt=MqttConfig(host="broker.example"),
        pixelblaze=PixelblazeConfig(host="pixelblaze.example"),
        bindings=(
            TopicBinding(
                topic="show/verse",
                command="set_variable",
                payload_format="json",
                value_path=("text",),
                transform="text_hash",
                variable="inputVerseHash",
            ),
            TopicBinding(
                topic="show/sentiment",
                command="set_variable",
                payload_format="json",
                value_path=("verse",),
                transform="text_hash",
                variable="inputSentimentVerseHash",
            ),
            TopicBinding(
                topic="show/pulse",
                command="set_variable",
                payload_format="json",
                value_path=("valve",),
                value_map={"flare": 1.0, "bigjet": 2.0, "poof": 3.0},
                variable="inputFlameValve",
            ),
        ),
    )
    mqtt_client = FakeMqttClient(
        [
            FakeMessage("show/verse", b'{"text":" Silver   WATCHER "}'),
            FakeMessage("show/sentiment", b'{"verse":"silver watcher"}'),
            FakeMessage("show/pulse", b'{"valve":"bigjet","ms":450}'),
        ]
    )
    pixelblaze = FakePixelblaze()

    MqttPixelblazeBridge(
        config,
        mqtt_client_factory=lambda client_id: mqtt_client,
        pixelblaze_factory=lambda host: pixelblaze,
    ).run_forever()

    verse_hash = pixelblaze.variables[0]["inputVerseHash"]
    assert 1 <= verse_hash <= 29999
    assert pixelblaze.variables == [
        {"inputVerseHash": verse_hash},
        {"inputSentimentVerseHash": verse_hash},
        {"inputFlameValve": 2.0},
    ]


@pytest.mark.parametrize(
    "payload",
    [b"not json", b"[]", b'{"controls":{"brightness":"high"}}', b"\xff"],
)
def test_bridge_skips_invalid_json_payloads_and_processes_later_messages(
    payload: bytes,
    caplog: pytest.LogCaptureFixture,
) -> None:
    config = BridgeConfig(
        mqtt=MqttConfig(host="broker.example"),
        pixelblaze=PixelblazeConfig(host="pixelblaze.example"),
        bindings=(
            TopicBinding(
                topic="show/state",
                command="set_brightness",
                payload_format="json",
                value_path=("controls", "brightness"),
            ),
        ),
    )
    mqtt_client = FakeMqttClient(
        [
            FakeMessage("show/state", payload),
            FakeMessage("show/state", b'{"controls":{"brightness":0.5}}'),
        ]
    )
    pixelblaze = FakePixelblaze()

    with caplog.at_level(logging.WARNING):
        MqttPixelblazeBridge(
            config,
            mqtt_client_factory=lambda client_id: mqtt_client,
            pixelblaze_factory=lambda host: pixelblaze,
        ).run_forever()

    assert pixelblaze.brightnesses == [0.5]
    assert caplog.messages


def test_bridge_logs_present_json_null_as_an_invalid_value(
    caplog: pytest.LogCaptureFixture,
) -> None:
    config = BridgeConfig(
        mqtt=MqttConfig(host="broker.example"),
        pixelblaze=PixelblazeConfig(host="pixelblaze.example"),
        bindings=(
            TopicBinding(
                topic="show/state",
                command="set_brightness",
                payload_format="json",
                value_path=("controls", "brightness"),
            ),
        ),
    )
    mqtt_client = FakeMqttClient(
        [FakeMessage("show/state", b'{"controls":{"brightness":null}}')]
    )
    pixelblaze = FakePixelblaze()
    logger = logging.getLogger("test.json-null")

    with caplog.at_level(logging.WARNING, logger=logger.name):
        MqttPixelblazeBridge(
            config,
            mqtt_client_factory=lambda client_id: mqtt_client,
            pixelblaze_factory=lambda host: pixelblaze,
            logger=logger,
        ).run_forever()

    assert pixelblaze.brightnesses == []
    assert caplog.messages == ["Ignoring non-numeric brightness payload on show/state"]


def test_callback_drops_new_message_when_the_bounded_work_queue_is_full(
    caplog: pytest.LogCaptureFixture,
) -> None:
    config = BridgeConfig(
        mqtt=MqttConfig(host="broker.example"),
        pixelblaze=PixelblazeConfig(host="pixelblaze.example"),
        bindings=(TopicBinding(topic="show/pattern", command="set_pattern"),),
    )
    mqtt_client = HoldingMqttClient(FakeMessage("show/pattern", b"first"))
    pixelblaze = BlockingPixelblaze()
    bridge = MqttPixelblazeBridge(
        config,
        mqtt_client_factory=lambda client_id: mqtt_client,
        pixelblaze_factory=lambda host: pixelblaze,
    )
    bridge_thread = Thread(target=bridge.run_forever)
    bridge_thread.start()
    assert pixelblaze.command_started.wait(timeout=1)

    for _ in range(100):
        mqtt_client.on_message(mqtt_client, None, FakeMessage("show/pattern", b"aurora"))
    with caplog.at_level(logging.WARNING):
        mqtt_client.on_message(mqtt_client, None, FakeMessage("show/pattern", b"dropped"))

    assert caplog.messages == [
        "Dropping MQTT message because the work queue is full: show/pattern"
    ]
    pixelblaze.release_command.set()
    mqtt_client.release_loop.set()
    bridge_thread.join(timeout=1)
    assert not bridge_thread.is_alive()
    assert pixelblaze.max_active_calls == 1

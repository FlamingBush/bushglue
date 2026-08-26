from __future__ import annotations

import json
import signal
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from mqtt_pixelblaze_bridge.cli import main
from mqtt_pixelblaze_bridge.config import ArrayLookup, ConfigurationError, load_config


def test_check_config_validates_without_constructing_a_bridge(tmp_path: Path, capsys) -> None:
    config_path = tmp_path / "bridge.json"
    config_path.write_text(
        json.dumps(
            {
                "mqtt": {"host": "broker.example"},
                "pixelblaze": {"host": "pixelblaze.example"},
                "bindings": [
                    {
                        "topic": "show/pattern",
                        "command": "set_pattern",
                        "payload_format": "scalar",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    def bridge_factory(*_args, **_kwargs):
        raise AssertionError("--check-config must not construct a bridge")

    assert main(["--config", str(config_path), "--check-config"], bridge_factory) == 0
    assert capsys.readouterr().out == "Configuration is valid.\n"


def test_configuration_validation_reports_multiple_actionable_errors(tmp_path: Path) -> None:
    config_path = tmp_path / "invalid.json"
    config_path.write_text(
        json.dumps(
            {
                "unexpected": True,
                "mqtt": {
                    "host": "",
                    "port": 0,
                    "password": "secret",
                },
                "pixelblaze": {"host": 12},
                "bindings": [],
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ConfigurationError) as raised:
        load_config(config_path)

    assert str(raised.value).splitlines() == [
        "unexpected: unknown setting.",
        "mqtt.host: must be a non-empty string.",
        "mqtt.port: must be an integer from 1 through 65535.",
        "mqtt.password: requires mqtt.username.",
        "pixelblaze.host: must be a non-empty string.",
        "bindings: must be a non-empty array.",
    ]


def test_configuration_loader_retains_optional_broker_settings(tmp_path: Path) -> None:
    config_path = tmp_path / "broker-settings.json"
    config_path.write_text(
        json.dumps(
            {
                "mqtt": {
                    "host": "broker.example",
                    "port": 1884,
                    "client_id": "odroid-lights",
                    "username": "lights",
                    "password": "not-logged",
                },
                "pixelblaze": {"host": "pixelblaze.example"},
                "bindings": [
                    {"topic": "show/pattern", "command": "set_pattern"}
                ],
            }
        ),
        encoding="utf-8",
    )

    config = load_config(config_path)

    assert config.mqtt.host == "broker.example"
    assert config.mqtt.port == 1884
    assert config.mqtt.client_id == "odroid-lights"
    assert config.mqtt.username == "lights"
    assert config.mqtt.password == "not-logged"


def test_configuration_loader_supports_all_scalar_commands(tmp_path: Path) -> None:
    config_path = tmp_path / "commands.json"
    config_path.write_text(
        json.dumps(
            {
                "mqtt": {"host": "broker.example"},
                "pixelblaze": {"host": "pixelblaze.example"},
                "bindings": [
                    {
                        "topic": "show/pattern",
                        "command": "set_pattern",
                        "value_map": {"start": "aurora"},
                    },
                    {"topic": "show/brightness", "command": "set_brightness"},
                    {
                        "topic": "show/speed",
                        "command": "set_variable",
                        "variable": "speed",
                    },
                ],
            }
        ),
        encoding="utf-8",
    )

    config = load_config(config_path)

    assert config.bindings[0].value_map == {"start": "aurora"}
    assert config.bindings[1].command == "set_brightness"
    assert config.bindings[2].variable == "speed"


def test_configuration_rejects_invalid_command_specific_binding_fields(tmp_path: Path) -> None:
    config_path = tmp_path / "invalid-bindings.json"
    config_path.write_text(
        json.dumps(
            {
                "mqtt": {"host": "broker.example"},
                "pixelblaze": {"host": "pixelblaze.example"},
                "bindings": [
                    {"topic": "a", "command": "unknown"},
                    {"topic": "b", "command": "set_variable"},
                    {
                        "topic": "c",
                        "command": "set_brightness",
                        "variable": "speed",
                        "value_map": {"on": "aurora"},
                    },
                    {
                        "topic": "d",
                        "command": "set_pattern",
                        "value_map": {"on": 3},
                    },
                    {
                        "topic": "e",
                        "command": "set_pattern",
                        "value_map": None,
                    },
                ],
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ConfigurationError) as raised:
        load_config(config_path)

    assert str(raised.value).splitlines() == [
        "bindings[0].command: must be set_pattern, set_brightness, set_variable, or trigger.",
        "bindings[1].variable: must be a non-empty string.",
        "bindings[2].variable: is only valid for set_variable or trigger.",
        "bindings[2].value_map: is only valid for set_pattern or set_variable.",
        "bindings[3].value_map.on: must be a non-empty string.",
        "bindings[4].value_map: must be a non-empty object of strings.",
    ]


def test_configuration_supports_json_array_lookups_and_event_triggers(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "sentiment.json"
    config_path.write_text(
        json.dumps(
            {
                "mqtt": {"host": "broker.example"},
                "pixelblaze": {"host": "pixelblaze.example"},
                "bindings": [
                    {
                        "topic": "show/sentiment",
                        "command": "set_variable",
                        "payload_format": "json",
                        "value_path": ["classification"],
                        "array_lookup": {
                            "match_key": "label",
                            "match_value": "joy",
                            "value_key": "score",
                        },
                        "variable": "inputJoy",
                    },
                    {
                        "topic": "show/sentiment",
                        "command": "trigger",
                        "payload_format": "json",
                        "variable": "sentimentTrigger",
                    },
                ],
            }
        ),
        encoding="utf-8",
    )

    config = load_config(config_path)

    assert config.bindings[0].array_lookup == ArrayLookup(
        match_key="label",
        match_value="joy",
        value_key="score",
    )
    assert config.bindings[1].command == "trigger"
    assert config.bindings[1].value_path is None


def test_configuration_supports_text_hashes_and_numeric_variable_maps(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "transforms.json"
    config_path.write_text(
        json.dumps(
            {
                "mqtt": {"host": "broker.example"},
                "pixelblaze": {"host": "pixelblaze.example"},
                "bindings": [
                    {
                        "topic": "show/verse",
                        "command": "set_variable",
                        "payload_format": "json",
                        "value_path": ["text"],
                        "transform": "text_hash",
                        "variable": "inputVerseHash",
                    },
                    {
                        "topic": "show/pulse",
                        "command": "set_variable",
                        "payload_format": "json",
                        "value_path": ["valve"],
                        "value_map": {"flare": 1, "bigjet": 2, "poof": 3},
                        "variable": "inputFlameValve",
                    },
                ],
            }
        ),
        encoding="utf-8",
    )

    config = load_config(config_path)

    assert config.bindings[0].transform == "text_hash"
    assert config.bindings[1].value_map == {
        "flare": 1.0,
        "bigjet": 2.0,
        "poof": 3.0,
    }


def test_configuration_rejects_invalid_variable_transforms_and_maps(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "invalid-transforms.json"
    config_path.write_text(
        json.dumps(
            {
                "mqtt": {"host": "broker.example"},
                "pixelblaze": {"host": "pixelblaze.example"},
                "bindings": [
                    {
                        "topic": "show/verse",
                        "command": "set_variable",
                        "variable": "verseHash",
                        "transform": "sha256",
                    },
                    {
                        "topic": "show/pattern",
                        "command": "set_pattern",
                        "transform": "text_hash",
                    },
                    {
                        "topic": "show/pulse",
                        "command": "set_variable",
                        "variable": "valve",
                        "value_map": {"flare": "one", "bigjet": True},
                    },
                    {
                        "topic": "show/both",
                        "command": "set_variable",
                        "variable": "value",
                        "value_map": {"one": 1},
                        "transform": "text_hash",
                    },
                ],
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ConfigurationError) as raised:
        load_config(config_path)

    assert str(raised.value).splitlines() == [
        "bindings[0].transform: must be text_hash.",
        "bindings[1].transform: is only valid for set_variable.",
        "bindings[2].value_map.flare: must be a finite number.",
        "bindings[2].value_map.bigjet: must be a finite number.",
        "bindings[3]: value_map and transform cannot be combined.",
    ]


def test_configuration_rejects_invalid_array_lookup_and_trigger_fields(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "invalid-events.json"
    config_path.write_text(
        json.dumps(
            {
                "mqtt": {"host": "broker.example"},
                "pixelblaze": {"host": "pixelblaze.example"},
                "bindings": [
                    {
                        "topic": "show/event",
                        "command": "trigger",
                        "payload_format": "json",
                        "value_path": ["ts"],
                    },
                    {
                        "topic": "show/sentiment",
                        "command": "set_variable",
                        "variable": "inputJoy",
                        "array_lookup": {},
                    },
                    {
                        "topic": "show/sentiment",
                        "command": "set_variable",
                        "payload_format": "json",
                        "value_path": ["classification"],
                        "array_lookup": {
                            "match_key": "",
                            "match_value": "joy",
                            "value_key": "score",
                            "unexpected": True,
                        },
                        "variable": "inputJoy",
                    },
                ],
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ConfigurationError) as raised:
        load_config(config_path)

    assert str(raised.value).splitlines() == [
        "bindings[0].value_path: is not valid for trigger.",
        "bindings[0].variable: must be a non-empty string.",
        "bindings[1].array_lookup: is only valid for json payloads.",
        "bindings[2].array_lookup.unexpected: unknown setting.",
        "bindings[2].array_lookup.match_key: must be a non-empty string.",
    ]


def test_configuration_requires_a_non_empty_object_path_for_json_bindings(tmp_path: Path) -> None:
    config_path = tmp_path / "invalid-json-bindings.json"
    config_path.write_text(
        json.dumps(
            {
                "mqtt": {"host": "broker.example"},
                "pixelblaze": {"host": "pixelblaze.example"},
                "bindings": [
                    {
                        "topic": "show/state",
                        "command": "set_pattern",
                        "payload_format": "json",
                    },
                    {
                        "topic": "show/state",
                        "command": "set_brightness",
                        "payload_format": "json",
                        "value_path": ["controls", 1],
                    },
                    {
                        "topic": "show/state",
                        "command": "set_variable",
                        "payload_format": "scalar",
                        "value_path": ["controls", "speed"],
                        "variable": "speed",
                    },
                ],
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ConfigurationError) as raised:
        load_config(config_path)

    assert str(raised.value).splitlines() == [
        "bindings[0].value_path: must be a non-empty array of non-empty strings.",
        "bindings[1].value_path[1]: must be a non-empty string.",
        "bindings[2].value_path: is only valid for json payloads.",
    ]


def test_cli_routes_sigterm_to_bridge_stop(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config_path = tmp_path / "bridge.json"
    config_path.write_text(
        json.dumps(
            {
                "mqtt": {"host": "broker.example"},
                "pixelblaze": {"host": "pixelblaze.example"},
                "bindings": [{"topic": "show/pattern", "command": "set_pattern"}],
            }
        ),
        encoding="utf-8",
    )
    handlers: dict[int, object] = {}

    def fake_signal(signum: int, handler: object) -> object:
        previous = handlers.get(signum, signal.SIG_DFL)
        handlers[signum] = handler
        return previous

    monkeypatch.setattr(signal, "signal", fake_signal)

    class FakeBridge:
        stopped = False

        def run_forever(self) -> None:
            handlers[signal.SIGTERM](signal.SIGTERM, None)  # type: ignore[operator]

        def stop(self) -> None:
            self.stopped = True

    bridge = FakeBridge()

    assert main(["--config", str(config_path)], lambda config: bridge) == 0
    assert bridge.stopped is True

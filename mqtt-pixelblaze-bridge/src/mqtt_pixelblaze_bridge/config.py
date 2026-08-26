from __future__ import annotations

import json
import math
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast


@dataclass(frozen=True)
class MqttConfig:
    host: str
    port: int = 1883
    client_id: str | None = None
    username: str | None = None
    password: str | None = None


@dataclass(frozen=True)
class PixelblazeConfig:
    host: str


@dataclass(frozen=True)
class ArrayLookup:
    match_key: str
    match_value: str
    value_key: str


@dataclass(frozen=True)
class TopicBinding:
    topic: str
    command: str
    payload_format: str = "scalar"
    variable: str | None = None
    value_map: Mapping[str, str | float] | None = None
    value_path: tuple[str, ...] | None = None
    array_lookup: ArrayLookup | None = None
    transform: str | None = None


@dataclass(frozen=True)
class BridgeConfig:
    mqtt: MqttConfig
    pixelblaze: PixelblazeConfig
    bindings: tuple[TopicBinding, ...]


class ConfigurationError(ValueError):
    def __init__(self, errors: list[str] | str) -> None:
        self.errors = [errors] if isinstance(errors, str) else errors
        super().__init__("\n".join(self.errors))


def load_config(path: str | Path) -> BridgeConfig:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except OSError as error:
        raise ConfigurationError(f"Unable to read configuration: {error}") from error
    except json.JSONDecodeError as error:
        raise ConfigurationError(f"Invalid JSON: {error.msg}") from error

    return parse_config(data)


def parse_config(data: Any) -> BridgeConfig:
    if not isinstance(data, Mapping):
        raise ConfigurationError("Configuration must be a JSON object.")

    errors: list[str] = []
    _validate_keys(data, {"mqtt", "pixelblaze", "bindings"}, "", errors)

    mqtt_data = _object_value(data, "mqtt", errors)
    pixelblaze_data = _object_value(data, "pixelblaze", errors)
    bindings_data = data.get("bindings")

    mqtt = _parse_mqtt(mqtt_data, errors)
    pixelblaze = _parse_pixelblaze(pixelblaze_data, errors)

    if not isinstance(bindings_data, list) or not bindings_data:
        errors.append("bindings: must be a non-empty array.")
        bindings_data = []

    bindings = tuple(
        _parse_binding(binding, index, errors)
        for index, binding in enumerate(bindings_data)
    )

    if errors:
        raise ConfigurationError(errors)

    assert mqtt is not None
    assert pixelblaze is not None
    return BridgeConfig(mqtt=mqtt, pixelblaze=pixelblaze, bindings=bindings)


def validate_config(config: BridgeConfig) -> BridgeConfig:
    return parse_config(
        {
            "mqtt": {
                "host": config.mqtt.host,
                "port": config.mqtt.port,
                "client_id": config.mqtt.client_id,
                "username": config.mqtt.username,
                "password": config.mqtt.password,
            },
            "pixelblaze": {"host": config.pixelblaze.host},
            "bindings": [
                _binding_data(binding)
                for binding in config.bindings
            ],
        }
    )


def _binding_data(binding: TopicBinding) -> dict[str, Any]:
    data: dict[str, Any] = {
        "topic": binding.topic,
        "command": binding.command,
        "payload_format": binding.payload_format,
    }
    if binding.variable is not None:
        data["variable"] = binding.variable
    if binding.value_map is not None:
        data["value_map"] = binding.value_map
    if binding.value_path is not None:
        data["value_path"] = list(binding.value_path)
    if binding.array_lookup is not None:
        data["array_lookup"] = {
            "match_key": binding.array_lookup.match_key,
            "match_value": binding.array_lookup.match_value,
            "value_key": binding.array_lookup.value_key,
        }
    if binding.transform is not None:
        data["transform"] = binding.transform
    return data


def _parse_mqtt(
    data: Mapping[str, Any] | None,
    errors: list[str],
) -> MqttConfig | None:
    if data is None:
        return None

    _validate_keys(data, {"host", "port", "client_id", "username", "password"}, "mqtt", errors)
    host = _non_empty_string(data.get("host"), "mqtt.host", errors)
    port = _port_value(data.get("port", 1883), errors)
    client_id = _optional_string(data.get("client_id"), "mqtt.client_id", errors)
    username = _optional_string(data.get("username"), "mqtt.username", errors)
    password = _optional_string(data.get("password"), "mqtt.password", errors)

    if password is not None and username is None:
        errors.append("mqtt.password: requires mqtt.username.")

    if host is None or port is None:
        return None

    return MqttConfig(
        host=host,
        port=port,
        client_id=client_id,
        username=username,
        password=password,
    )


def _parse_pixelblaze(
    data: Mapping[str, Any] | None,
    errors: list[str],
) -> PixelblazeConfig | None:
    if data is None:
        return None

    _validate_keys(data, {"host"}, "pixelblaze", errors)
    host = _non_empty_string(data.get("host"), "pixelblaze.host", errors)
    if host is None:
        return None

    return PixelblazeConfig(host=host)


def _object_value(
    data: Mapping[str, Any],
    key: str,
    errors: list[str],
) -> Mapping[str, Any] | None:
    value = data.get(key)
    if not isinstance(value, Mapping):
        errors.append(f"{key}: must be an object.")
        return None
    return value


def _parse_binding(
    data: Any,
    index: int,
    errors: list[str],
) -> TopicBinding:
    prefix = f"bindings[{index}]"
    if not isinstance(data, Mapping):
        errors.append(f"{prefix}: must be an object.")
        return TopicBinding(topic="", command="set_pattern")

    _validate_keys(
        data,
        {
            "topic",
            "command",
            "payload_format",
            "variable",
            "value_map",
            "value_path",
            "array_lookup",
            "transform",
        },
        prefix,
        errors,
    )
    topic = _non_empty_string(data.get("topic"), f"{prefix}.topic", errors)
    command = _non_empty_string(data.get("command"), f"{prefix}.command", errors)
    payload_format = data.get("payload_format", "scalar")
    variable: str | None = None
    value_map: Mapping[str, str | float] | None = None
    value_path: tuple[str, ...] | None = None
    array_lookup: ArrayLookup | None = None
    transform: str | None = None

    if command not in {"set_pattern", "set_brightness", "set_variable", "trigger", None}:
        errors.append(
            f"{prefix}.command: must be set_pattern, set_brightness, "
            "set_variable, or trigger."
        )
    if payload_format not in {"scalar", "json"}:
        errors.append(f"{prefix}.payload_format: must be scalar or json.")

    if payload_format == "json":
        if command != "trigger":
            value_path = _value_path(
                data.get("value_path"),
                f"{prefix}.value_path",
                errors,
            )
        elif "value_path" in data:
            errors.append(f"{prefix}.value_path: is not valid for trigger.")
    elif "value_path" in data:
        errors.append(f"{prefix}.value_path: is only valid for json payloads.")

    if "array_lookup" in data:
        if payload_format != "json":
            errors.append(f"{prefix}.array_lookup: is only valid for json payloads.")
        elif command == "trigger":
            errors.append(f"{prefix}.array_lookup: is not valid for trigger.")
        else:
            array_lookup = _array_lookup(
                data["array_lookup"],
                f"{prefix}.array_lookup",
                errors,
            )

    if "transform" in data:
        transform = _non_empty_string(
            data["transform"],
            f"{prefix}.transform",
            errors,
        )
        if transform is not None and transform != "text_hash":
            errors.append(f"{prefix}.transform: must be text_hash.")
        if command != "set_variable":
            errors.append(f"{prefix}.transform: is only valid for set_variable.")

    if command == "set_pattern":
        if "variable" in data:
            errors.append(
                f"{prefix}.variable: is only valid for set_variable or trigger."
            )
        if "value_map" in data:
            value_map = _string_map(data["value_map"], f"{prefix}.value_map", errors)
    elif command == "set_variable":
        variable = _non_empty_string(data.get("variable"), f"{prefix}.variable", errors)
        if "value_map" in data:
            value_map = _number_map(data["value_map"], f"{prefix}.value_map", errors)
        if "value_map" in data and "transform" in data:
            errors.append(f"{prefix}: value_map and transform cannot be combined.")
    elif command == "trigger":
        variable = _non_empty_string(data.get("variable"), f"{prefix}.variable", errors)
        if "value_map" in data:
            errors.append(
                f"{prefix}.value_map: is only valid for set_pattern or set_variable."
            )
    elif command == "set_brightness":
        if "variable" in data:
            errors.append(
                f"{prefix}.variable: is only valid for set_variable or trigger."
            )
        if "value_map" in data:
            errors.append(
                f"{prefix}.value_map: is only valid for set_pattern or set_variable."
            )

    return TopicBinding(
        topic=topic or "",
        command=command or "set_pattern",
        payload_format=payload_format if isinstance(payload_format, str) else "scalar",
        variable=variable,
        value_map=value_map,
        value_path=value_path,
        array_lookup=array_lookup,
        transform=transform,
    )


def _array_lookup(
    value: Any,
    location: str,
    errors: list[str],
) -> ArrayLookup | None:
    if not isinstance(value, Mapping):
        errors.append(f"{location}: must be an object.")
        return None

    _validate_keys(
        value,
        {"match_key", "match_value", "value_key"},
        location,
        errors,
    )
    match_key = _non_empty_string(
        value.get("match_key"),
        f"{location}.match_key",
        errors,
    )
    match_value = _non_empty_string(
        value.get("match_value"),
        f"{location}.match_value",
        errors,
    )
    value_key = _non_empty_string(
        value.get("value_key"),
        f"{location}.value_key",
        errors,
    )
    if match_key is None or match_value is None or value_key is None:
        return None
    return ArrayLookup(
        match_key=match_key,
        match_value=match_value,
        value_key=value_key,
    )


def _string_map(
    value: Any,
    location: str,
    errors: list[str],
) -> Mapping[str, str] | None:
    if not isinstance(value, Mapping) or not value:
        errors.append(f"{location}: must be a non-empty object of strings.")
        return None

    result: dict[str, str] = {}
    for key, mapped_value in value.items():
        if not isinstance(key, str) or not key:
            errors.append(f"{location}: keys must be non-empty strings.")
            continue
        if not isinstance(mapped_value, str) or not mapped_value:
            errors.append(f"{location}.{key}: must be a non-empty string.")
            continue
        result[key] = mapped_value
    return result


def _number_map(
    value: Any,
    location: str,
    errors: list[str],
) -> Mapping[str, str | float] | None:
    if not isinstance(value, Mapping) or not value:
        errors.append(f"{location}: must be a non-empty object of finite numbers.")
        return None

    result: dict[str, str | float] = {}
    for key, mapped_value in value.items():
        if not isinstance(key, str) or not key:
            errors.append(f"{location}: keys must be non-empty strings.")
            continue
        if (
            isinstance(mapped_value, bool)
            or not isinstance(mapped_value, (int, float))
            or not math.isfinite(mapped_value)
        ):
            errors.append(f"{location}.{key}: must be a finite number.")
            continue
        result[key] = float(mapped_value)
    return result


def _value_path(value: Any, location: str, errors: list[str]) -> tuple[str, ...] | None:
    if not isinstance(value, list) or not value:
        errors.append(f"{location}: must be a non-empty array of non-empty strings.")
        return None

    path: list[str] = []
    for index, key in enumerate(value):
        if not isinstance(key, str) or not key:
            errors.append(f"{location}[{index}]: must be a non-empty string.")
            continue
        path.append(key)
    return tuple(path)


def _validate_keys(
    data: Mapping[str, Any],
    allowed: set[str],
    prefix: str,
    errors: list[str],
) -> None:
    for key in data:
        if key not in allowed:
            location = f"{prefix}.{key}" if prefix else key
            errors.append(f"{location}: unknown setting.")


def _non_empty_string(value: Any, location: str, errors: list[str]) -> str | None:
    if not isinstance(value, str) or not value:
        errors.append(f"{location}: must be a non-empty string.")
        return None
    return value


def _optional_string(value: Any, location: str, errors: list[str]) -> str | None:
    if value is None:
        return None
    return _non_empty_string(value, location, errors)


def _port_value(value: Any, errors: list[str]) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 65535:
        errors.append("mqtt.port: must be an integer from 1 through 65535.")
        return None
    return cast(int, value)

from __future__ import annotations

import json
import logging
import math
import time
from collections.abc import Callable, Mapping
from queue import Empty, Full, Queue
from threading import Event, Thread
from typing import Any

from .config import BridgeConfig, TopicBinding, validate_config


class MqttPixelblazeBridge:
    def __init__(
        self,
        config: BridgeConfig,
        *,
        mqtt_client_factory: Callable[[str | None], Any] | None = None,
        pixelblaze_factory: Callable[[str], Any] | None = None,
        logger: logging.Logger | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._config = validate_config(config)
        self._mqtt_client_factory = mqtt_client_factory or self._create_mqtt_client
        self._pixelblaze_factory = pixelblaze_factory or self._create_pixelblaze_client
        self._logger = logger or logging.getLogger(__name__)
        self._clock = clock
        self._mqtt_client: Any | None = None
        self._pixelblaze_client: Any | None = None
        self._stopped = False
        self._last_pattern: str | None = None
        self._work_queue: Queue[tuple[str, bytes] | None] = Queue(maxsize=100)
        self._worker: Thread | None = None
        self._worker_started = Event()
        self._stopped_event = Event()
        self._next_pixelblaze_attempt = 0.0
        self._pixelblaze_retry_delay = 1.0
        self._trigger_value: float | None = None

    def run_forever(self) -> None:
        self._start()
        self._stopped_event.wait()

    def _start(self) -> None:
        self._mqtt_client = self._mqtt_client_factory(self._config.mqtt.client_id)
        self._mqtt_client.on_message = self._on_message
        self._mqtt_client.on_connect = self._on_connect
        self._mqtt_client.on_subscribe = self._on_subscribe
        self._mqtt_client.suppress_exceptions = True
        self._start_worker()

        if self._config.mqtt.username is not None:
            self._mqtt_client.username_pw_set(
                self._config.mqtt.username,
                self._config.mqtt.password,
            )

        self._mqtt_client.reconnect_delay_set(min_delay=1, max_delay=30)
        self._mqtt_client.connect_async(
            self._config.mqtt.host,
            self._config.mqtt.port,
            60,
        )
        self._mqtt_client.loop_start()

    def stop(self) -> None:
        if self._stopped:
            return

        self._stopped = True
        self._stopped_event.set()
        if self._mqtt_client is not None:
            self._mqtt_client.disconnect()
            self._mqtt_client.loop_stop()
        if self._worker is not None:
            self._worker_started.wait()
            while True:
                try:
                    self._work_queue.get_nowait()
                except Exception:
                    break
                else:
                    self._work_queue.task_done()
            self._work_queue.put(None)
            self._worker.join()

    def _on_connect(
        self,
        client: Any,
        userdata: Any,
        flags: Any,
        reason_code: Any,
        properties: Any = None,
    ) -> None:
        del userdata, flags, properties
        if reason_code != 0:
            self._logger.warning("MQTT connection rejected: %s", reason_code)
            return
        for topic in dict.fromkeys(binding.topic for binding in self._config.bindings):
            try:
                result = client.subscribe(topic, qos=0)
                if isinstance(result, tuple) and result[0] != 0:
                    self._logger.warning("MQTT subscription rejected for %s: %s", topic, result[0])
            except Exception:
                self._logger.exception("MQTT subscription failed for %s", topic)

    def _on_subscribe(
        self,
        client: Any,
        userdata: Any,
        mid: Any,
        reason_codes: Any,
        properties: Any = None,
    ) -> None:
        del client, userdata, mid, properties
        if any(code != 0 for code in reason_codes):
            self._logger.warning("MQTT subscription acknowledgement rejected: %s", reason_codes)

    def _on_message(self, client: Any, userdata: Any, message: Any) -> None:
        del client, userdata
        if self._stopped:
            self._logger.warning("Dropping MQTT message during shutdown: %s", message.topic)
            return
        try:
            self._work_queue.put_nowait((str(message.topic), bytes(message.payload)))
        except Full:
            self._logger.warning(
                "Dropping MQTT message because the work queue is full: %s",
                message.topic,
            )
        except Exception:
            self._logger.exception("Unable to queue MQTT message on %s", message.topic)

    def _start_worker(self) -> None:
        self._worker = Thread(target=self._run_worker, name="pixelblaze-worker")
        self._worker.start()

    def _run_worker(self) -> None:
        self._worker_started.set()
        try:
            while True:
                timeout = None
                if self._pixelblaze_client is None:
                    timeout = max(0.0, self._next_pixelblaze_attempt - self._clock())
                try:
                    work = self._work_queue.get(timeout=timeout)
                except Empty:
                    self._try_create_pixelblaze()
                    continue
                try:
                    if work is None:
                        return
                    if self._pixelblaze_client is None:
                        if not self._try_create_pixelblaze():
                            self._logger.warning(
                                "Pixelblaze unavailable; dropping MQTT message on %s",
                                work[0],
                            )
                            continue
                    self._process_message(*work)
                except Exception:
                    topic = work[0] if work is not None else "shutdown"
                    self._logger.exception("Unable to process queued MQTT message on %s", topic)
                finally:
                    self._work_queue.task_done()
        finally:
            self._dispose_pixelblaze()

    def _try_create_pixelblaze(self) -> bool:
        if self._clock() < self._next_pixelblaze_attempt:
            return False
        try:
            self._pixelblaze_client = self._pixelblaze_factory(self._config.pixelblaze.host)
        except Exception as error:
            delay = self._schedule_pixelblaze_retry()
            self._logger.warning(
                "Pixelblaze connection failed; retrying in %.0f seconds: %s",
                delay,
                error,
            )
            return False
        self._pixelblaze_retry_delay = 1.0
        self._next_pixelblaze_attempt = 0.0
        return True

    def _schedule_pixelblaze_retry(self) -> float:
        delay = self._pixelblaze_retry_delay
        self._next_pixelblaze_attempt = self._clock() + delay
        self._pixelblaze_retry_delay = min(delay * 2, 30.0)
        return delay

    def _dispose_pixelblaze(self) -> None:
        if self._pixelblaze_client is None:
            return
        exit_context = getattr(self._pixelblaze_client, "__exit__", None)
        if callable(exit_context):
            exit_context(None, None, None)
        self._pixelblaze_client = None
        self._last_pattern = None

    def _process_message(self, topic: str, payload: bytes) -> None:
        matching_bindings = [
            binding
            for binding in self._config.bindings
            if binding.topic == topic
        ]
        if not matching_bindings:
            return

        try:
            text = payload.decode("utf-8")
        except UnicodeDecodeError:
            self._logger.warning("Ignoring non-UTF-8 payload on %s", topic)
            return

        if not text:
            self._logger.warning("Ignoring empty scalar payload on %s", topic)
            return

        json_payload: Mapping[str, Any] | None = None
        if any(binding.payload_format == "json" for binding in matching_bindings):
            try:
                decoded_json = json.loads(text)
            except json.JSONDecodeError:
                self._logger.warning("Ignoring malformed JSON payload on %s", topic)
                decoded_json = None
            if decoded_json is not None and not isinstance(decoded_json, Mapping):
                self._logger.warning("Ignoring non-object JSON payload on %s", topic)
            elif isinstance(decoded_json, Mapping):
                json_payload = decoded_json

        for binding in matching_bindings:
            if binding.payload_format == "scalar":
                value: Any = None if binding.command == "trigger" else text
                self._dispatch_value(binding, value, topic)
            elif json_payload is not None:
                if binding.command == "trigger":
                    self._dispatch_value(binding, None, topic)
                else:
                    found, value = self._extract_json_value(binding, json_payload, topic)
                    if found:
                        self._dispatch_value(binding, value, topic)

    def _dispatch_value(self, binding: TopicBinding, value: Any, topic: str) -> None:
        assert self._pixelblaze_client is not None
        try:
            if binding.command == "set_pattern":
                pattern = self._mapped_pattern(binding, value, topic)
                if pattern is None or pattern == self._last_pattern:
                    return
                self._pixelblaze_client.setActivePatternByName(pattern)
                self._last_pattern = pattern
            elif binding.command == "set_brightness":
                brightness = self._finite_number(value, topic, "brightness")
                if brightness is None:
                    return
                if not 0.0 <= brightness <= 1.0:
                    self._logger.warning(
                        "Ignoring out-of-range brightness payload on %s", topic
                    )
                    return
                self._pixelblaze_client.setBrightnessSlider(brightness)
            elif binding.command == "set_variable":
                variable_value = self._variable_number(binding, value, topic)
                if variable_value is None:
                    return
                assert binding.variable is not None
                self._pixelblaze_client.setActiveVariables(
                    {binding.variable: variable_value}
                )
            elif binding.command == "trigger":
                assert binding.variable is not None
                self._pixelblaze_client.setActiveVariables(
                    {binding.variable: self._next_trigger_value()}
                )
        except Exception:
            self._logger.exception(
                "Pixelblaze %s command failed for %s", binding.command, topic
            )
            self._dispose_pixelblaze()
            self._schedule_pixelblaze_retry()

    def _mapped_pattern(
        self,
        binding: TopicBinding,
        value: Any,
        topic: str,
    ) -> str | None:
        if not isinstance(value, str) or not value:
            self._logger.warning("Ignoring non-string pattern payload on %s", topic)
            return None
        if binding.value_map is None:
            return value
        pattern = binding.value_map.get(value)
        if not isinstance(pattern, str):
            self._logger.warning("Ignoring unmapped pattern payload on %s", topic)
            return None
        return pattern

    def _variable_number(
        self,
        binding: TopicBinding,
        value: Any,
        topic: str,
    ) -> float | None:
        if binding.transform == "text_hash":
            return self._normalized_text_hash(value, topic)
        if binding.value_map is not None:
            if not isinstance(value, str):
                self._logger.warning(
                    "Ignoring non-string mapped variable payload on %s",
                    topic,
                )
                return None
            mapped_value = binding.value_map.get(value)
            if not isinstance(mapped_value, (int, float)):
                self._logger.warning("Ignoring unmapped variable payload on %s", topic)
                return None
            return float(mapped_value)
        return self._finite_number(value, topic, "variable")

    def _normalized_text_hash(self, value: Any, topic: str) -> float | None:
        if not isinstance(value, str):
            self._logger.warning("Ignoring non-string text_hash payload on %s", topic)
            return None
        normalized = " ".join(value.split()).casefold()
        if not normalized:
            self._logger.warning("Ignoring empty text_hash payload on %s", topic)
            return None

        # FNV-1a reduced to a positive integer that is exactly representable by
        # Pixelblaze v2's signed 16.16 fixed-point numbers.
        hash_value = 2166136261
        for byte in normalized.encode("utf-8"):
            hash_value ^= byte
            hash_value = (hash_value * 16777619) & 0xFFFFFFFF
        return float(hash_value % 29999 + 1)

    def _finite_number(self, value: Any, topic: str, command: str) -> float | None:
        if isinstance(value, bool) or not isinstance(value, (str, int, float)):
            self._logger.warning("Ignoring non-numeric %s payload on %s", command, topic)
            return None
        try:
            number = float(value)
        except ValueError:
            self._logger.warning("Ignoring non-numeric %s payload on %s", command, topic)
            return None
        if not math.isfinite(number):
            self._logger.warning("Ignoring non-finite %s payload on %s", command, topic)
            return None
        return number

    def _extract_json_value(
        self,
        binding: TopicBinding,
        payload: Mapping[str, Any],
        topic: str,
    ) -> tuple[bool, Any]:
        assert binding.value_path is not None
        value: Any = payload
        for key in binding.value_path:
            if not isinstance(value, Mapping) or key not in value:
                self._logger.warning("Missing JSON path for %s on %s", binding.command, topic)
                return False, None
            value = value[key]
        if binding.array_lookup is not None:
            return self._extract_array_lookup(binding, value, topic)
        return True, value

    def _extract_array_lookup(
        self,
        binding: TopicBinding,
        value: Any,
        topic: str,
    ) -> tuple[bool, Any]:
        lookup = binding.array_lookup
        assert lookup is not None
        if not isinstance(value, list):
            self._logger.warning(
                "JSON path for %s on %s is not an array",
                binding.command,
                topic,
            )
            return False, None
        for item in value:
            if not isinstance(item, Mapping):
                continue
            if item.get(lookup.match_key) != lookup.match_value:
                continue
            if lookup.value_key not in item:
                self._logger.warning(
                    "Matched JSON array item is missing %s for %s on %s",
                    lookup.value_key,
                    binding.command,
                    topic,
                )
                return False, None
            return True, item[lookup.value_key]
        self._logger.warning(
            "No JSON array item matched %s=%s for %s on %s",
            lookup.match_key,
            lookup.match_value,
            binding.command,
            topic,
        )
        return False, None

    def _next_trigger_value(self) -> float:
        if self._trigger_value is None:
            self._trigger_value = float(int(self._clock() * 1000) % 29999)
        self._trigger_value = float(int(self._trigger_value) % 29999 + 1)
        return self._trigger_value

    @staticmethod
    def _create_mqtt_client(client_id: str | None) -> Any:
        import paho.mqtt.client as mqtt

        return mqtt.Client(
            mqtt.CallbackAPIVersion.VERSION2,
            client_id=client_id or "",
        )

    @staticmethod
    def _create_pixelblaze_client(host: str) -> Any:
        from pixelblaze import Pixelblaze  # type: ignore[attr-defined]
        from websocket import WebSocketTimeoutException

        class BridgePixelblaze(Pixelblaze):  # type: ignore[misc]
            def _connection_maint(self) -> None:
                try:
                    super()._connection_maint()
                except WebSocketTimeoutException:
                    # A v2 Pixelblaze can advertise a readable socket before a
                    # complete frame is available. The upstream client treats
                    # that initial buffer flush as fatal; later device calls
                    # retain normal error handling and bridge recovery.
                    return

        return BridgePixelblaze(host)

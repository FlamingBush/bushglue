"""MQTT contract wrapper for Bush Glue services.

Implements the retained `bush/<service>/status` (`offline`|`starting`|`ready`)
+ retained `bush/<service>/version` + non-retained `bush/<service>/fault`
contract from the autoplan eng review.

Why this exists
---------------
Before this wrapper, every failure mode in bushglue was communicated by
silence. A service crash, a model load hang, a broker blip — none of them
produced an MQTT signal a subscriber could see. Operators at the playa would
look at a healthy-looking systemd dashboard and have no idea the bush had
gone emotionally flat half an hour ago.

The contract this wrapper enforces:

- **Pre-connect:** `will_set` LWT publishes `status=offline` retained on
  ungraceful disconnect. The next subscriber to come along sees the truth.
- **On connect:** publish `status=starting` + `version=<git-short-hash>`
  retained, BEFORE returning. Subscribe order is set up here too.
- **After model load / warmup:** caller invokes `mark_ready()`. Status flips
  to `ready` retained. `_ready_latched=True` so reconnects re-publish ready.
- **On controlled shutdown:** caller invokes `disconnect()`. Status flips
  back to `offline` explicitly before disconnect, overriding LWT.
- **On error:** caller invokes `publish_fault(error_type, context)`. Goes
  out non-retained on `bush/<service>/fault` so subscribers see live faults
  but the topic doesn't accumulate stale errors after the next deploy.

Design constraints
------------------
- Uses paho-mqtt v2 CallbackAPIVersion.VERSION2 to match existing services.
- `reconnect_delay_set(min=1, max=60)` so transient broker blips self-heal.
- Re-subscribes inside `on_connect` so SUBACK ordering is correct (the
  integration test's race fix piggybacks on this).
- Explicit version detection: `BUSH_VERSION` env var > git short hash > "dev".
  Avoids forcing every systemd unit to set WorkingDirectory.
- Subscriber handlers are isolated: an exception in one handler is caught
  + published as a fault, not allowed to break the MQTT loop.

Out of scope (separate P0 items)
--------------------------------
- Sentiment's daemon-thread silent-death fix (P0 #3 part 2 — depends on
  this wrapper but is a behavioral change in how loop_forever is hosted).
- Pico firmware MQTT contract (P0 #1 — different language, different idiom).
"""
from __future__ import annotations

import json
import os
import subprocess
import time
from typing import Callable, Optional

import paho.mqtt.client as mqtt

from . import get_mqtt_broker


def _default_version() -> str:
    """Best-effort version: BUSH_VERSION env var > git short hash > 'dev'."""
    env_v = os.environ.get("BUSH_VERSION")
    if env_v:
        return env_v.strip()
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            timeout=2,
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
    except (subprocess.TimeoutExpired, OSError, FileNotFoundError):
        pass
    return "dev"


class MqttServiceClient:
    """Wrap paho-mqtt with the bush/<svc>/{status,version,fault} contract.

    Usage:
        client = MqttServiceClient(service_name="sentiment")
        client.subscribe("bush/pipeline/t2v/verse", on_verse)
        client.subscribe("bush/pipeline/tts/done", on_done)
        client.connect()              # publishes status=starting via on_connect
        # ...load model...
        client.mark_ready()           # publishes status=ready
        client.loop_forever()
    """

    def __init__(
        self,
        service_name: str,
        broker: Optional[str] = None,
        port: int = 1883,
        keepalive: int = 60,
        version: Optional[str] = None,
    ):
        self.service_name = service_name
        self.broker = broker or get_mqtt_broker()
        self.port = port
        self.keepalive = keepalive
        self.version = version if version is not None else _default_version()

        self.status_topic = f"bush/{service_name}/status"
        self.version_topic = f"bush/{service_name}/version"
        self.fault_topic = f"bush/{service_name}/fault"

        self._ready_latched = False
        self._handlers: dict[str, Callable] = {}
        self._subscriptions: list[tuple[str, int]] = []
        self._user_on_connect: Optional[Callable] = None
        self._user_on_disconnect: Optional[Callable] = None

        self._client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
        # LWT: published retained by the broker on ungraceful disconnect.
        self._client.will_set(self.status_topic, "offline", qos=1, retain=True)
        # Auto-reconnect for broker blips. paho re-fires on_connect after each.
        self._client.reconnect_delay_set(min_delay=1, max_delay=60)
        self._client.on_connect = self._on_connect
        self._client.on_message = self._dispatch_message
        self._client.on_disconnect = self._on_disconnect

    # ── public API ─────────────────────────────────────────────────────────

    def subscribe(self, topic: str, handler: Callable, qos: int = 0) -> None:
        """Register a handler for *topic*. Subscribed inside on_connect.

        Handlers are called as ``handler(client, msg)`` where ``client`` is
        the underlying paho client. Exceptions are caught and re-published
        as faults; they do not propagate to the MQTT loop.
        """
        self._subscriptions.append((topic, qos))
        self._handlers[topic] = handler

    def set_on_connect(self, callback: Callable) -> None:
        """Optional hook fired AFTER the contract publishes + subscribes."""
        self._user_on_connect = callback

    def set_on_disconnect(self, callback: Callable) -> None:
        self._user_on_disconnect = callback

    def mark_ready(self) -> None:
        """Publish status=ready retained. Latches across reconnects."""
        self._ready_latched = True
        self._client.publish(self.status_topic, "ready", qos=1, retain=True)

    def publish_fault(self, error_type: str, context: Optional[dict] = None) -> None:
        """Publish a non-retained fault event for live operator alerting."""
        payload = {"error": error_type, "ts": time.time()}
        if context:
            payload.update(context)
        self._client.publish(
            self.fault_topic, json.dumps(payload), qos=1, retain=False
        )

    def publish(self, topic: str, payload, qos: int = 0, retain: bool = False):
        """Pass-through publish for non-contract topics."""
        return self._client.publish(topic, payload, qos=qos, retain=retain)

    def connect(self) -> None:
        """Connect to the broker. Triggers our on_connect → status=starting."""
        self._client.connect(self.broker, self.port, self.keepalive)

    def loop_forever(self) -> None:
        """Run the MQTT loop in the calling thread. Returns only on disconnect()."""
        self._client.loop_forever()

    def loop_start(self) -> None:
        """Run the MQTT loop in a background paho-managed thread."""
        self._client.loop_start()

    def loop_stop(self) -> None:
        self._client.loop_stop()

    def disconnect(self) -> None:
        """Controlled shutdown: publish status=offline, then disconnect.

        Overrides LWT: this is the graceful-shutdown signal, not a crash.
        """
        try:
            self._client.publish(self.status_topic, "offline", qos=1, retain=True)
        except Exception:
            pass
        self._client.disconnect()

    @property
    def client(self) -> mqtt.Client:
        """Underlying paho client. Use sparingly; bypasses contract."""
        return self._client

    @property
    def ready_latched(self) -> bool:
        return self._ready_latched

    # ── internal callbacks ─────────────────────────────────────────────────

    def _on_connect(self, client, userdata, flags, reason_code, properties):
        # status=starting + version on every connect (including reconnects).
        client.publish(self.status_topic, "starting", qos=1, retain=True)
        client.publish(self.version_topic, self.version, qos=1, retain=True)
        # Subscribe inside on_connect so subscriptions survive reconnects.
        for topic, qos in self._subscriptions:
            client.subscribe(topic, qos=qos)
        # If we'd already marked ready, re-publish — the broker may have
        # lost retained state during the disconnect window.
        if self._ready_latched:
            client.publish(self.status_topic, "ready", qos=1, retain=True)
        if self._user_on_connect:
            self._user_on_connect(client, userdata, flags, reason_code, properties)

    def _on_disconnect(self, client, userdata, *args):
        # paho v2 callback signature varies (disconnect_flags, reason_code,
        # properties). We don't care which form — just chain to user hook.
        if self._user_on_disconnect:
            self._user_on_disconnect(client, userdata, *args)

    def _dispatch_message(self, client, userdata, msg):
        handler = self._handlers.get(msg.topic)
        if handler is None:
            return
        try:
            handler(client, msg)
        except Exception as e:
            self.publish_fault(
                "message_handler_error",
                {"topic": msg.topic, "error_class": type(e).__name__, "error_message": str(e)},
            )

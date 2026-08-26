from __future__ import annotations

import argparse
import signal
from collections.abc import Callable
from typing import Any, Sequence

from .bridge import MqttPixelblazeBridge
from .config import BridgeConfig, ConfigurationError, load_config


def main(
    argv: Sequence[str] | None = None,
    bridge_factory: Callable[[BridgeConfig], Any] = MqttPixelblazeBridge,
) -> int:
    parser = argparse.ArgumentParser(prog="mqtt-pixelblaze-bridge")
    parser.add_argument("--config", required=True, help="Path to the JSON configuration file")
    parser.add_argument(
        "--check-config",
        action="store_true",
        help="Validate configuration without connecting to MQTT or Pixelblaze",
    )
    arguments = parser.parse_args(argv)

    try:
        config = load_config(arguments.config)
    except ConfigurationError as error:
        parser.error(str(error))

    if arguments.check_config:
        print("Configuration is valid.")
        return 0

    bridge = bridge_factory(config)

    def stop_bridge(_signum: int, _frame: Any) -> None:
        bridge.stop()

    previous_sigint = signal.signal(signal.SIGINT, stop_bridge)
    previous_sigterm = signal.signal(signal.SIGTERM, stop_bridge)
    try:
        bridge.run_forever()
    finally:
        signal.signal(signal.SIGINT, previous_sigint)
        signal.signal(signal.SIGTERM, previous_sigterm)
    return 0

# MQTT-to-Pixelblaze Bridge

A standalone Python package that subscribes to configured MQTT topics and
translates their payloads into Pixelblaze pattern, brightness, variable, and
event-trigger commands.

The bridge itself is installation-independent. The included Emotional Fire
directory is a complete example with real message shapes, two Pixelblaze scene
sources, commissioning instructions, and a local MQTT smoke publisher.

## Project contents

| Path | Purpose |
| --- | --- |
| `src/mqtt_pixelblaze_bridge/` | Reusable Python package and CLI. |
| `docs/mqtt-pixelblaze-bridge.md` | Configuration, operation, Odroid deployment, and troubleshooting guide. |
| `docs/mqtt-pixelblaze-bridge.service` | Example systemd service. |
| `examples/bridge.example.json` | Small installation-neutral configuration. |
| `examples/emotional-fire/` | Complete Emotional Fire configuration and smoke publisher. |
| `pixelblaze/` | Spiral and unordered Emotional Fire scene source. |
| `tests/` | Package, configuration, lifecycle, and example-contract tests. |

## Install and run

From this directory:

```sh
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --constraint constraints.txt .
mqtt-pixelblaze-bridge \
  --config examples/bridge.example.json \
  --check-config
mqtt-pixelblaze-bridge --config /path/to/bridge.json
```

Edit the broker and Pixelblaze addresses before a live run. See the
[operator guide](docs/mqtt-pixelblaze-bridge.md) for the complete configuration
matrix and deployment procedure.

## Emotional Fire example

Start with the [Emotional Fire commissioning guide](examples/emotional-fire/README.md).
It documents scene installation, the real MQTT bindings, emotion-combination
comparisons, optional observed flame-pulse overlays, and safe dry runs.

```sh
python examples/emotional-fire/smoke_test.py \
  --dry-run \
  --layout unordered \
  --scenario emotions \
  --with-flame-pulses \
  --pause-between-tests
```

## Development and packaging

Install all local verification tools and run the checks from this directory:

```sh
python -m pip install -e '.[test,build]'
python -m pytest
python -m mypy src
python -m build
```

The source distribution contains the operator docs, service unit, examples,
tests, and Pixelblaze sources. The wheel contains the runtime Python package and
the `mqtt-pixelblaze-bridge` command.

## Moving into a monorepo

Copy this entire directory into the target repository, preserving its internal
layout. It does not depend on the parent repository's Python configuration or
working directory. Install it using its relative path, for example:

```sh
python -m pip install ./projects/mqtt-pixelblaze-bridge
```

Monorepo tooling can invoke tests with:

```sh
python -m pytest projects/mqtt-pixelblaze-bridge/tests
python -m mypy --config-file projects/mqtt-pixelblaze-bridge/pyproject.toml \
  projects/mqtt-pixelblaze-bridge/src
```

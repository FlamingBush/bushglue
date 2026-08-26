# MQTT-to-Pixelblaze Bridge

This package runs one MQTT-to-Pixelblaze bridge instance: one MQTT broker, one Pixelblaze device, and one or more topic bindings. It has no MOOT-specific settings or compatibility behavior.

## Foreground quick start

Create a Python 3.10 or newer virtual environment, activate it, and install the package:

```sh
python3 -m venv .venv
source .venv/bin/activate
python -m pip install .
```

Create `bridge.json`:

```json
{
  "mqtt": {
    "host": "192.168.0.200"
  },
  "pixelblaze": {
    "host": "192.168.0.205"
  },
  "bindings": [
    {
      "topic": "installation/pattern",
      "command": "set_pattern",
      "payload_format": "scalar"
    },
    {
      "topic": "installation/state",
      "command": "set_pattern",
      "payload_format": "json",
      "value_path": ["scene", "name"]
    },
    {
      "topic": "installation/state",
      "command": "set_brightness",
      "payload_format": "json",
      "value_path": ["controls", "brightness"]
    }
  ]
}
```

Validate configuration without contacting either endpoint:

```sh
mqtt-pixelblaze-bridge --config bridge.json --check-config
```

Run the bridge in the foreground:

```sh
mqtt-pixelblaze-bridge --config bridge.json
```

From another machine that can reach the broker, publish a pattern name:

```sh
mosquitto_pub -h 192.168.0.200 -t installation/pattern -m aurora
```

The Pixelblaze must contain a pattern named `aurora`. Invalid JSON configuration, empty pattern messages, and non-UTF-8 pattern messages are rejected before a pattern is selected.

## Configuration reference

Minimal scalar configuration:

```json
{
  "mqtt": {
    "host": "192.168.0.200"
  },
  "pixelblaze": {
    "host": "192.168.0.205"
  },
  "bindings": [
    {
      "topic": "installation/pattern",
      "command": "set_pattern"
    }
  ]
}
```

Complete scalar-and-object configuration:

```json
{
  "mqtt": {
    "host": "192.168.0.200",
    "client_id": "odroid-lights",
    "username": "lights",
    "password": "change-me"
  },
  "pixelblaze": {
    "host": "192.168.0.205"
  },
  "bindings": [
    {
      "topic": "installation/pattern",
      "command": "set_pattern",
      "value_map": {
        "open": "aurora"
      }
    },
    {
      "topic": "installation/brightness",
      "command": "set_brightness"
    },
    {
      "topic": "installation/speed",
      "command": "set_variable",
      "variable": "speed"
    },
    {
      "topic": "installation/state",
      "command": "set_pattern",
      "payload_format": "json",
      "value_path": ["scene"]
    },
    {
      "topic": "installation/state",
      "command": "set_brightness",
      "payload_format": "json",
      "value_path": ["controls", "brightness"]
    },
    {
      "topic": "installation/state",
      "command": "set_variable",
      "payload_format": "json",
      "value_path": ["controls", "speed"],
      "variable": "speed"
    }
  ]
}
```

`mqtt.host` and `pixelblaze.host` are required non-empty strings. `mqtt.port` defaults to `1883`; `client_id`, `username`, and `password` are optional, though a password requires a username. Protect a configuration containing credentials with `chmod 600 bridge.json`.

Each binding requires a `topic` and one of `set_pattern`, `set_brightness`, `set_variable`, or `trigger`. `payload_format` defaults to `scalar`. For a value-producing JSON binding, `value_path` is a required non-empty array of object-property names. It does not index arrays or implement JSONPath. An optional `array_lookup` can select the first object in the array at `value_path` whose `match_key` equals `match_value`, then return its `value_key`.

`set_variable` and `trigger` require a non-empty `variable`. `set_pattern` optionally accepts a non-empty string-to-string `value_map`; `set_variable` optionally accepts a string-to-number `value_map`. A variable binding can instead use `"transform": "text_hash"` to turn normalized text into a compact numeric correlation token. `value_map` and `transform` cannot be combined.

A JSON trigger validates that the MQTT payload is an object but does not require `value_path`. Each valid trigger message advances one bridge-wide event counter and sends it to the named variable, allowing repeated events with identical payloads to remain observable by a Pixelblaze pattern. Most patterns should only test whether a trigger changed. The shared order also lets a pattern with wrap-aware comparison resolve lifecycle events from different topics that arrive within one render frame, as the Emotional Fire example does.

Brightness accepts finite values from 0 through 1. Variable values must be finite numbers unless a numeric `value_map` or `text_hash` transform converts an input string. A repeated successful pattern is suppressed; brightness, variable, and trigger commands are always dispatched. Pixelblaze variable names that do not exist may be ignored by the upstream client.

| Configuration path | Required | Default | Valid values / notes |
| --- | --- | --- | --- |
| `mqtt.host` | Yes | — | Non-empty broker hostname or IP address. |
| `mqtt.port` | No | `1883` | Integer from `1` through `65535`. |
| `mqtt.client_id` | No | Paho default | Non-empty string when supplied. |
| `mqtt.username` | No | — | Non-empty string when supplied. Required when `mqtt.password` is supplied. |
| `mqtt.password` | No | — | Non-empty string when supplied; keep the configuration file mode `600`. |
| `pixelblaze.host` | Yes | — | Non-empty Pixelblaze hostname or IP address. |
| `bindings` | Yes | — | Non-empty array of binding objects. Duplicate topics are subscribed once; their bindings run in listed order. |
| `bindings[].topic` | Yes | — | Non-empty MQTT topic string. |
| `bindings[].command` | Yes | — | `set_pattern`, `set_brightness`, `set_variable`, or `trigger`. |
| `bindings[].payload_format` | No | `scalar` | `scalar` for a UTF-8 payload, or `json` for a JSON object. |
| `bindings[].value_path` | Value-producing JSON only | — | Non-empty array of non-empty object-key strings. Required for JSON pattern, brightness, and variable bindings; not accepted by triggers. |
| `bindings[].array_lookup` | No | — | JSON-only selector applied to the array at `value_path`; contains `match_key`, `match_value`, and `value_key` strings. The first matching object wins. |
| `bindings[].array_lookup.match_key` | With `array_lookup` | — | Object property compared in each array item. |
| `bindings[].array_lookup.match_value` | With `array_lookup` | — | Non-empty string the match property must equal. |
| `bindings[].array_lookup.value_key` | With `array_lookup` | — | Object property containing the dispatched value. |
| `bindings[].value_map` | Pattern/variable only | — | For patterns, a non-empty string-to-string object. For variables, a non-empty string-to-finite-number object. An unmapped value is logged and skipped. |
| `bindings[].transform` | Variable only | — | Currently only `text_hash`. Cannot be combined with `value_map`. |
| `bindings[].variable` | Variable/trigger only | — | Required non-empty Pixelblaze active-variable name. |

| Command | Accepted scalar value | Accepted JSON value at `value_path` | Dispatch behavior |
| --- | --- | --- | --- |
| `set_pattern` | Non-empty UTF-8 string | Non-empty JSON string | Applies `value_map` when configured; successful exact repeats are suppressed. |
| `set_brightness` | Finite number from `0.0` to `1.0` | JSON number from `0.0` to `1.0` | Invalid values are logged and skipped, never clamped. |
| `set_variable` | Finite number, or mapped/transformed string | Finite JSON number, or mapped/transformed JSON string | Sends `{variable: value}` to the active pattern; repeated values are still sent. |
| `trigger` | Any non-empty UTF-8 payload | Any valid JSON object | Advances and sends an event counter; the payload value itself is not dispatched. |

The bridge logs malformed payloads, missing JSON paths, queue overflow, connection/subscription failures, and device errors. Dispatch means the command was handed to the Pixelblaze client; it is not a device acknowledgement.

`text_hash` collapses whitespace and applies Unicode case folding before hashing. It returns an integer from 1 through 29999 so the value is exactly representable on Pixelblaze v2. It is useful for best-effort equality checks such as matching two copies of a verse, but its small range permits collisions; it is not an identity, security, or deduplication mechanism.

## Emotional Fire example

The standalone project includes two Pixelblaze v2/v3 pattern sources, a complete binding configuration for the real pipeline topics, and a local publisher that exercises normal completion, four contrasting emotion combinations, the missing-done watchdog, event-ordering races, and optional observed flame pulses. Its optional `--pause-between-tests` flag provides a 30-second observation interval between cases. Both patterns use the complete pixel count configured on the device; their buffered render structure also avoids a v2 physical-output stall observed with complex per-pixel calculations. Start with [`examples/emotional-fire/README.md`](../examples/emotional-fire/README.md).

Bindings sharing one topic execute in listed order. In the Emotional Fire configuration, the six `input*` scores and normalized sentiment-verse hash deliberately precede `sentimentTrigger`: the scene consumes the complete score set only after the trigger changes and only when the verse matches the latest `bush/pipeline/t2v/verse` message. `bush/pipeline/tts/speaking` arms done, while a newer verse disarms it so a likely stale `bush/pipeline/tts/done` cannot release the new scene. The speaking watchdog defaults to 30 seconds.

The sentiment payload's `flare` and `bigjet` fields remain ignored because they are vestigial. Separate imperative commands on `bush/flame/pulse` are observed to duck the LEDs during physical flame and add a cool rebound. Those bindings are visual-only: the bridge never publishes flame commands or controls relay hardware.

## Odroid installation and service

This package supports 64-bit `aarch64` Debian 11 or Ubuntu 20.04 and newer with Python 3.10+. 32-bit ARM is unsupported. Before installing, verify `uname -m` reports `aarch64`, check `/etc/os-release`, and run `python3 --version`.

```sh
sudo useradd --system --home /opt/mqtt-pixelblaze-bridge --shell /usr/sbin/nologin bridge
sudo mkdir -p /opt/mqtt-pixelblaze-bridge /etc/mqtt-pixelblaze-bridge
sudo chown bridge:bridge /opt/mqtt-pixelblaze-bridge
sudo -u bridge python3 -m venv /opt/mqtt-pixelblaze-bridge/.venv
sudo -u bridge /opt/mqtt-pixelblaze-bridge/.venv/bin/python -m pip install --upgrade pip
sudo -u bridge /opt/mqtt-pixelblaze-bridge/.venv/bin/pip install --constraint constraints.txt .
/opt/mqtt-pixelblaze-bridge/.venv/bin/python -c 'import paho.mqtt.client, pixelblaze; print("clients import")'
sudo install -m 600 -o bridge -g bridge bridge.json /etc/mqtt-pixelblaze-bridge/bridge.json
sudo install -m 644 docs/mqtt-pixelblaze-bridge.service /etc/systemd/system/mqtt-pixelblaze-bridge.service
sudo systemctl daemon-reload
sudo systemctl enable --now mqtt-pixelblaze-bridge
```

Use `systemctl status`, `restart`, or `stop mqtt-pixelblaze-bridge` and inspect logs with `journalctl -u mqtt-pixelblaze-bridge -f`. Validate config before restarting after changes. A shutdown should finish within 30 seconds; inspect the journal if it does not.

## Smoke test and troubleshooting

Run these from a broker-reachable machine (add `-u`/`-P` when credentials are enabled):

```sh
mosquitto_pub -h 192.168.0.200 -t installation/pattern -m aurora
mosquitto_pub -h 192.168.0.200 -t installation/pattern -m open
mosquitto_pub -h 192.168.0.200 -t installation/brightness -m 0.5
mosquitto_pub -h 192.168.0.200 -t installation/speed -m 1.25
mosquitto_pub -h 192.168.0.200 -t installation/state -m '{"scene":"aurora","controls":{"brightness":0.5,"speed":1.25}}'
mosquitto_pub -h 192.168.0.200 -t installation/state -m '{bad json}'
mosquitto_pub -h 192.168.0.200 -t installation/state -m '{"scene":"aurora","controls":{"brightness":0.5,"speed":1.25}}'
sudo systemctl restart mosquitto
sudo systemctl restart mqtt-pixelblaze-bridge
```

After a broker restart, confirm resubscription in the journal. Temporarily make Pixelblaze unreachable, publish a command, restore it, and publish a later command to confirm recovery. This real Odroid, broker, and Pixelblaze smoke test is the manual acceptance boundary; the repository suite is hardware-independent.

For failures, first verify architecture and Python/pip, then broker address and credentials, subscription permissions, topic names, and Pixelblaze reachability. Logs identify rejected subscriptions, malformed values, missing patterns, ignored variables, reconnects, queue drops, and shutdown problems.

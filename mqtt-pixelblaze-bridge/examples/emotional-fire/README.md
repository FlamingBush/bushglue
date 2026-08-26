# Emotional Fire local smoke test

This example drives either of two Pixelblaze patterns intended for the
18-fixture installation from the real sentiment, TTS, and flame-pulse message
shapes. Both patterns render the full pixel count configured on the Pixelblaze,
so they can also use an arbitrary-length strip during commissioning. The smoke
test exercises the two pipeline races the lighting controller can detect
without an utterance ID: late sentiment and a likely stale `tts/done` after a
newer verse.

The `flare` and `bigjet` fields inside a sentiment result remain vestigial and
are ignored. Actual commands on `bush/flame/pulse` are observed only to shape
the LEDs; this bridge never publishes a valve command or controls a relay.

## 1. Install a Pixelblaze pattern

In the Pixelblaze web editor, create a new pattern and paste one source file.
The supplied local configuration currently maps both layout choices to
`bush-scene`, matching the single scene used for commissioning. Save the chosen
source as `bush-scene` to use that configuration unchanged.

| Source | Suggested name if retaining both scenes | Physical assumption |
| --- | --- | --- |
| `pixelblaze/emotional_fire_spiral.js` | `Emotional Fire - Spiral` | Indexes run along the spiral. Direction is reversible with `centerAtPixelZero`. |
| `pixelblaze/emotional_fire_unordered.js` | `Emotional Fire - Unordered` | No relationship between index order and fixture position. |

To keep both scenes on one controller, save them under the suggested names and
change the `bush/lights/layout` binding's `value_map` in `bridge.local.json` to
those same names.

Configure the Pixelblaze output for the number of individually addressable
pixels actually connected (18 for the planned installation) and the correct
protocol/color order for the fixtures. Both sources size their frame buffers
from Pixelblaze's `pixelCount`; neither clamps output to 18. Reselect the pattern
after changing the device's pixel count so its buffers are initialized for the
new span. Start with a conservative device-level brightness or current limit;
the smoke test requests 45% global brightness.

The supplied patterns calculate the whole frame in `beforeRender()` and keep
`render()` intentionally small. This avoids a Pixelblaze v2 failure mode seen
during commissioning where the browser preview continued to animate but a
complex per-pixel physical output stopped partway through the strip. Preserve
this buffered structure when modifying either scene.

The source syntax and exported variables work on Pixelblaze v2 firmware 2.29.
After saving, open the variable watcher. These diagnostics are useful during
commissioning:

| Variable | Meaning |
| --- | --- |
| `sceneMode` | `0` idle, `1` anticipating, `2` speaking, or `3` releasing. |
| `doneArmed` | `1` only after a speaking event. A new verse or release resets it to `0`. |
| `lastSentimentMatched` | `1` when the most recent sentiment verse matched the active T2V verse; otherwise `0`. |
| `releaseWasWatchdog` | `1` when anticipation or speaking timed out; `0` for a valid done release. |
| `lastFlameValve` | Most recently observed valve: `1` flare, `2` bigjet, or `3` poof. |
| `emotionLevel` | Strength of the emotional palette, from `1` toward the idle state at `0`. |

The patterns use top-level exported variables as the network control surface,
as described by the official [Pixelblaze WebSocket API](https://electromage.com/docs/websockets-api/).
Their syntax is limited to the v2-compatible subset in the official
[language reference](https://electromage.com/docs/language-reference/).

After saving the pattern, close other Pixelblaze browser tabs and stop any
previous bridge with Ctrl-C before starting this example. A v2 controller is
most reliable when one long-lived client owns its WebSocket connection.

## 2. Configure and start the local bridge

`bridge.local.json` assumes the local Mosquitto broker is at `127.0.0.1` and
the Pixelblaze is at `192.168.1.99`. Edit those two addresses if needed. The
pipeline bindings use these real topics:

- `bush/pipeline/t2v/verse`
- `bush/pipeline/sentiment/result`
- `bush/pipeline/tts/speaking`
- `bush/pipeline/tts/done`
- `bush/flame/pulse`

From the standalone project root:

```sh
source .venv/bin/activate
python -m pip install --force-reinstall --no-deps .
brew services start mosquitto
mqtt-pixelblaze-bridge \
  --config examples/emotional-fire/bridge.local.json \
  --check-config
mqtt-pixelblaze-bridge \
  --config examples/emotional-fire/bridge.local.json
```

Leave the bridge running in that terminal. Bindings for one MQTT topic execute
in their listed order. Do not move a topic's trigger ahead of its staging
bindings: the trigger tells the scene that all fields for that message have
already been sent.

### Topic and variable matrix

| MQTT topic | Payload consumed | Staged variables, then action |
| --- | --- | --- |
| `bush/pipeline/t2v/verse` | `text` | Hashes normalized text into `inputVerseHash`, then advances `verseTrigger`. Starts anticipation, makes this the active verse, and disarms done. |
| `bush/pipeline/sentiment/result` | `verse` and the unsorted `classification` array | Stages all six `input*` scores plus `inputSentimentVerseHash`, then advances `sentimentTrigger`. The scene accepts the scores only when the verse hashes match. Sentiment `flare` and `bigjet` fields are ignored. |
| `bush/pipeline/tts/speaking` | Any valid JSON object | Advances `speakingTrigger`, enters speaking, arms done, and starts the speaking watchdog. Its `text` is intentionally not used for strict correlation. |
| `bush/pipeline/tts/done` | Any valid JSON object | Advances `doneTrigger`. Releases only when done is armed, so a likely stale done after a newer verse is ignored. |
| `bush/flame/pulse` | `valve` and `ms` | Maps flare/bigjet/poof to `1`/`2`/`3`, stages the visual duration, then advances `flamePulseTrigger`. This is an observational LED effect only. |
| `bush/lights/layout` | `spiral` or `unordered` | Selects the name in the binding's `value_map`; both values map to `bush-scene` in the supplied single-scene local configuration. |
| `bush/lights/brightness` | Number from `0` through `1` | Sets global Pixelblaze brightness. |
| `bush/lights/speaking-timeout-seconds` | Number; scene clamps to `5`–`300` | Changes the missing-done watchdog. |
| `bush/lights/anticipation-timeout-seconds` | Number; scene clamps to `5`–`180` | Releases if speaking never starts. |
| `bush/lights/decay-seconds` | Number; scene clamps to `5`–`180` | Changes the release-to-base duration. |
| `bush/lights/spiral-center-at-zero` | `1` or `0` | Uses fixture index 0 or the last index as the spiral center; ignored by the unordered pattern. |

The patterns default to a 30-second speaking watchdog, 30-second anticipation
watchdog, and 35-second decay. Trigger values come from one bridge-wide,
wrap-aware event sequence. This preserves cross-topic order even if multiple
updates land within one Pixelblaze render frame. Trigger values and text hashes
are not timestamps or scene parameters.

For best-effort sentiment correlation, the bridge collapses whitespace,
case-folds the T2V `text` and sentiment `verse`, and sends the same compact hash
for equivalent text. This prevents a stale sentiment result from replacing a
newer verse's palette. Hash collisions are possible, and events without an
utterance ID cannot be correlated perfectly; an ID propagated by every
publisher would still be the definitive future fix.

### Flame-pulse visual behavior

The flame stream is imperative, QoS 0, and non-retained. Each pulse is treated
as an observed event, not persistent state:

| Valve | LEDs while physically open | Cool rebound after the pulse |
| --- | --- | --- |
| `flare` | Brief 32% duck | Cyan/turquoise for about 1.2 seconds. |
| `bigjet` | Strong 68% duck so the flame dominates | Deeper electric blue for about 2.5 seconds. |
| `poof` | Brief 20% duck | Short cyan flicker for about 0.6 seconds. |

The visual duration is clamped to 50–5000 ms. Repeated pulses extend the
corresponding visual deadline and never shorten it; different valve envelopes
run concurrently. This clamp affects only the renderer and is not a safety
control for the relay.

## 3. Publish visible sequences

In a second terminal, run the normal completion path:

```sh
source .venv/bin/activate
python examples/emotional-fire/smoke_test.py \
  --layout spiral \
  --scenario normal \
  --spiral-center first
```

Expected sequence:

1. A low indigo/violet base continuously drifts and twinkles.
2. The verse produces an outer-to-center gathering effect.
3. The unsorted sentiment array produces mostly turquoise joy with orchid love accents.
4. Speaking holds outward waves.
5. Done produces one center-out exhale and starts the 35-second decay.

Use `--layout unordered` to test the layout-independent scene. It replaces
directional travel with distributed fixture phases.

### Compare emotion combinations

Run four contrasting emotion mixes without flame commands:

```sh
python examples/emotional-fire/smoke_test.py \
  --layout unordered \
  --scenario emotions
```

Each case publishes a complete verse, sentiment, speaking, and done lifecycle.
By default it remains in speaking mode for six seconds and leaves a two-second
release gap before the next case. `--hold-seconds` changes the six-second hold.
Add `--pause-between-tests` to replace each inter-case gap with 30 seconds,
allowing the scene to visibly decay before the next score combination:

```sh
python examples/emotional-fire/smoke_test.py \
  --layout unordered \
  --scenario emotions \
  --pause-between-tests
```

| Case | Strongest scores | What it compares |
| --- | --- | --- |
| Joy + love | joy `0.51`, love `0.34` | Turquoise energy with orchid/rose affinity. |
| Anger + surprise | anger `0.52`, surprise `0.33` | Ultraviolet/crimson tension against icy cyan flashes. |
| Fear + sadness | fear `0.46`, sadness `0.39` | Green-teal unease mixed with slow cobalt/indigo weight. |
| Balanced | love and surprise `0.18`, joy and sadness `0.17`, anger and fear `0.15` | Whether all six spatial color roles remain distinguishable without one dominant result. |

For a direct comparison, run the same four cases again with an identical flame
overlay on each one:

```sh
python examples/emotional-fire/smoke_test.py \
  --layout unordered \
  --scenario emotions \
  --with-flame-pulses \
  --pause-between-tests \
  --allow-flame-pulses
```

The overlay sends `flare` for 350 ms one second into each case, followed 750 ms
later by `bigjet` for 700 ms. These are real imperative commands on
`bush/flame/pulse`; use an isolated broker disconnected from live flame relays.
`--with-flame-pulses` enables the overlay, while `--allow-flame-pulses` is the
required live-run safety acknowledgement. A `--dry-run` prints the comparison
and any planned 30-second pauses without connecting, waiting, or requiring the
safety acknowledgement.

Test the missing-done fallback with a short eight-second watchdog:

```sh
python examples/emotional-fire/smoke_test.py \
  --layout spiral \
  --scenario watchdog \
  --watchdog-seconds 8
```

No done message is published. The watcher should show
`releaseWasWatchdog = 1`. The script restores the default 30-second speaking
timeout afterward.

Exercise the defensive ordering rules:

```sh
python examples/emotional-fire/smoke_test.py --scenario races
```

The script starts one utterance, begins a newer verse, sends the old done and
old sentiment, then sends speaking before the new matching sentiment. Watch for
the stale done to leave `sceneMode = 1`, the stale sentiment to set
`lastSentimentMatched = 0`, and the matching late sentiment to change it to `1`.

Exercise visual reactions to actual valve commands:

```sh
python examples/emotional-fire/smoke_test.py \
  --scenario flame \
  --allow-flame-pulses
```

This sends two overlapping flare pulses followed by concurrent bigjet and poof
commands. A relay subscriber on the same broker will receive them and can
operate real valves. Use a broker isolated from live flame hardware when running
this smoke scenario. The explicit flag is required because these MQTT messages
are real imperative valve commands.

Use `--scenario both` for normal plus watchdog, or `--scenario all` for every
scenario, including the emotion comparisons (`all` also requires
`--allow-flame-pulses` because it contains the standalone flame scenario). Add
`--with-flame-pulses` to `all` if the four emotion comparisons should also get
the identical overlay. `--pause-between-tests` also inserts 30 seconds between
the top-level cases selected by `both` or `all`. Add `--dry-run` to print every
MQTT message without connecting to a broker or waiting between events; the
safety acknowledgement is not required for a dry run.

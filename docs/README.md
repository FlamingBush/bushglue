# Bush Glue — Modular System for Critique of Intersections of Religion and Technology

See the [root README](../README.md) for setup and deploy instructions.

## Component Diagram

![MQTT architecture diagram](mqtt-architecture.png)

> Source: [`mqtt-architecture.dot`](mqtt-architecture.dot) — regenerate with `dot -Tpng mqtt-architecture.dot -o mqtt-architecture.png`

---

## Topics Reference

### Pipeline Topics

| Topic | Direction | Publisher | Subscribers |
|-------|-----------|-----------|-------------|
| `bush/pipeline/stt/transcript` | → | bush-stt | bush-t2v |
| `bush/pipeline/stt/partial` | → | bush-stt | (monitor/discord) |
| `bush/pipeline/t2v/processing` | → | bush-t2v | (monitor/discord) |
| `bush/pipeline/t2v/verse` | → | bush-t2v | bush-tts, bush-sentiment |
| `bush/pipeline/tts/speaking` | → | bush-tts | bush-stt |
| `bush/pipeline/tts/done` | → | bush-tts | bush-stt, bush-sentiment |
| `bush/pipeline/sentiment/result` | → | bush-sentiment | (monitor/discord) |
| `bush/pipeline/stt/force-finalize` | → | (external) | bush-stt |

### Fire Control Topics

| Topic | Direction | Publisher | Subscribers |
|-------|-----------|-----------|-------------|
| `bush/flame/pulse` | → | bush-sentiment, bush-firecontrol, bush-firecontrol-web | relay-control, sound-service |
| `bush/fire/safety/forced_off` | ← | relay-control (Pico firmware) | (monitor/discord) |

### Needle Valve Topics

| Topic | Direction | Publisher | Subscribers |
|-------|-----------|-----------|-------------|
| `bush/fire/valve/target` | → | bush-flame-expression, bush-valve | relay-control |
| `bush/fire/valve/home` | → | bush-valve, (external) | relay-control |
| `bush/fire/valve/stop` | → | bush-valve, (external) | relay-control |
| `bush/fire/valve/calibrate` | → | bush-valve, (external) | relay-control |
| `bush/fire/valve/actual` | ← | relay-control | bush-valve, (monitor) |
| `bush/fire/valve/status` | ← | relay-control | bush-valve, (monitor) |
| `bush/fire/valve/online` | ← | relay-control | (monitor) |

### Audio Management Topics (all retained)

| Topic | Direction | Publisher | Subscribers |
|-------|-----------|-----------|-------------|
| `bush/audio/devices` | → | audio-agent | (external tools) |
| `bush/audio/discover` | → | (external) | audio-agent |
| `bush/audio/stt/set-device` | → | (external) | bush-stt |
| `bush/audio/stt/device` | ← | bush-stt | (external tools) |
| `bush/audio/tts/set-device` | → | (external) | bush-tts |
| `bush/audio/tts/device` | ← | bush-tts | (external tools) |
| `bush/audio/tts/set-clarity` | → | (external) | bush-tts |
| `bush/audio/tts/clarity` | ← | bush-tts | (external tools) |

---

## Message Payloads

### `bush/pipeline/stt/transcript`
```json
{ "text": "the recognized speech", "ts": 1711234567.89 }
```

### `bush/pipeline/stt/partial`
```json
{ "text": "interim recognition in progress" }
```

### `bush/pipeline/t2v/processing`
```json
{ "text": "the query text", "ts": 1711234567.89 }
```

### `bush/pipeline/t2v/verse`
```json
{ "query": "original transcript", "text": "generated verse text", "ts": 1711234567.89 }
```

### `bush/pipeline/tts/speaking`
```json
{ "text": "the text being spoken", "ts": 1711234567.89 }
```

### `bush/pipeline/tts/done`
```json
{ "ts": 1711234567.89 }
```

### `bush/pipeline/sentiment/result`
```json
{
  "verse": "the classified verse text",
  "classification": [
    { "label": "joy",     "score": 0.82 },
    { "label": "anger",   "score": 0.07 },
    { "label": "love",    "score": 0.05 },
    { "label": "surprise","score": 0.03 },
    { "label": "fear",    "score": 0.02 },
    { "label": "sadness", "score": 0.01 }
  ],
  "flare":   350,
  "bigjet":  200,
  "ts": 1711234567.89
}
```
Labels are one of: `anger` `joy` `love` `surprise` `fear` `sadness`

### `bush/flame/pulse`
```json
{"valve": "flare", "ms": 350}
```
`valve` is one of: `flare`, `bigjet`, `poof`. `ms` is the duration to open the solenoid valve. Typical ranges:
- flare: 50–2000 ms
- bigjet: 100–1000 ms
- poof: 20–450 ms

### `bush/fire/valve/target`
```
0.500
```
Float 0.0 (closed) to 1.0 (fully open). Also accepts JSON `{"target": 0.5}`.

### `bush/fire/valve/actual`
```
0.423
```
Float 0.0–1.0, current fractional position. Published at 5 Hz during motion, 1 Hz when idle.

### `bush/fire/valve/status`
```json
{
  "state": "idle",
  "pos": 0.42,
  "target": 0.50,
  "homed": true,
  "stalled": false,
  "last_error": null
}
```
`state` is one of: `unknown`, `homing`, `idle`, `moving`, `stalled`, `error`.

### `bush/fire/valve/online`
```
online
```
Retained birth message. Value is `online` or `offline`.

### `bush/fire/safety/forced_off`
```json
{ "reason": "mqtt_open", "ts": 1711234567 }
```
Published by the Pico firmware whenever it force-offs all solenoids before
entering a blocking socket/wifi call. Non-retained. `reason` is one of
`wifi_connect`, `mqtt_open`, `tcp_probe`. `ts` is `supervisor.ticks_ms()`
on the Pico (29-day rolling counter — relative time, not wall-clock).
Operators correlate this with reconnect events to confirm a "stuck pulse"
report was actually a force-off-truncated pulse rather than a relay
hardware fault.

### `bush/fire/valve/home`
Empty payload. Triggers homing sequence (drive to open stop, zero).

### `bush/fire/valve/stop`
Empty payload. Emergency stop.

### `bush/fire/valve/calibrate`
```
16000
```
Integer: number of microstep pulses from fully open to fully closed. Takes effect immediately, does not persist across reboots.

### `bush/audio/devices` (retained)
```json
{
  "capture":  [{ "index": 0, "name": "USB Audio",    "channels": 1, "sr": 16000.0 }],
  "playback": [{ "index": 1, "name": "pulse",        "channels": 2, "sr": 44100.0 }]
}
```

### `bush/audio/stt/set-device`
```json
{ "device": 2 }
```
Value is a device index (int) or name (string).

### `bush/audio/stt/device` (retained)
```json
{ "device": 2, "status": "ok" }
```

### `bush/audio/tts/set-device`
```json
{ "device": "hw:1,0" }
```
Value is an ALSA device string or `null` for system default.

### `bush/audio/tts/device` (retained)
```json
{ "device": "hw:1,0", "status": "ok" }
```

### `bush/audio/tts/set-clarity`
```json
{ "clarity": 75 }
```
`0` = heavy reverb (voice-of-God), `100` = dry/intelligible.

### `bush/audio/tts/clarity` (retained)
```json
{ "clarity": 75, "status": "ok" }
```

### `bush/audio/discover`
Empty payload. Triggers audio-agent to re-scan and republish `bush/audio/devices`.

### `bush/pipeline/stt/force-finalize`
Empty payload. Forces bush-stt to finalize the current recognition window immediately.

---

## Data Flow

### Main pipeline
```
1. Mic audio  →  bush-stt (Vosk)
2. bush-stt   PUB  bush/pipeline/stt/transcript   {text, ts}
3. bush-t2v   SUB  bush/pipeline/stt/transcript  →  HTTP GET localhost:8765/query
                                                     (Ollama embed → ChromaDB lookup)
4. bush-t2v   PUB  bush/pipeline/t2v/verse        {query, text, ts}
5a. bush-tts  SUB  bush/pipeline/t2v/verse  →  espeak-ng + sox reverb  →  speaker
    bush-tts  PUB  bush/pipeline/tts/speaking  at start
    bush-tts  PUB  bush/pipeline/tts/done      at finish
5b. bush-sentiment SUB bush/pipeline/t2v/verse → DistilBERT classify
    bush-sentiment PUB bush/pipeline/sentiment/result
    bush-sentiment PUB bush/flame/pulse  {valve,ms}  (loop until bush/pipeline/tts/done)
6. bush-stt    SUB  bush/pipeline/tts/speaking  →  mute mic
   bush-stt    SUB  bush/pipeline/tts/done      →  unmute + reset Vosk
```

Steps 5a and 5b run in parallel from the same `t2v/verse` message.
The fire loop in bush-sentiment is bounded by `tts/done` or a 30 s safety timeout.

### Discord /pray injection
```
1. User  /pray "some words"  in Discord
2. discord-bot  →  utils/bush-pray --phrase "some words"
3. bush-pray    →  WAV synthesis  →  ALSA loopback hw:Loopback,0
4. bush-stt     captures loopback audio  →  normal pipeline from step 2 above
5. discord-bot  SUB all pipeline topics  →  embed updates in Discord channel
```

### Audio device configuration
```
1. audio-agent PUB bush/audio/devices (retained, updated on bush/audio/discover)
2. Operator     PUB bush/audio/stt/set-device  {device: N}
3. bush-stt     validates  →  PUB bush/audio/stt/device  {device, status} (retained)
   (same flow for bush/audio/tts/set-device, bush/audio/tts/set-clarity)
```

---

## Timing Constants

| Constant | Value | Meaning |
|----------|-------|---------|
| `MUTE_TIMEOUT_S` | 30 s | Auto-unmute STT if `tts/done` never arrives |
| `FIRE_MAX_SECONDS` | 30 s | Safety cutoff for fire loop in bush-sentiment |
| T_TRANSCRIPT (t2v) | 30 s | Timeout waiting for transcript after `processing` |
| T_VERSE (tts) | 45 s | Timeout waiting for verse |
| T_DONE (sentiment) | 90 s | Timeout from transcript to `tts/done` |

---

## Emotion → Fire Patterns

Bush-sentiment maps the top DistilBERT emotion label to a fire pattern. Each pattern
controls pulse duration and inter-pulse gap (with random jitter) for the two valves.

| Emotion | Flare (hum) | BigJet (whoosh) | Character |
|---------|-------------|-----------------|-----------|
| anger   | short, fast | medium, frequent | aggressive bursts |
| joy     | medium      | medium           | lively, bouncy |
| love    | long, slow  | rare             | warm, sustained |
| surprise| sharp, quick| occasional big   | startled spikes |
| fear    | flickering  | rare             | trembling |
| sadness | slow, long gaps | very rare    | low, mournful |

Confidence score scales the pulse durations linearly within each pattern's range.

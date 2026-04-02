# Bush Glue — Installation Guide

Bush Glue is the pipeline that connects speech-to-text → text-to-verse → sentiment
analysis → fire hardware control, running as independent systemd services communicating
over MQTT.

## Architecture

```
[Mic] → bush-stt → bush/pipeline/stt/transcript
                          ↓
                     bush-t2v → bush/pipeline/t2v/verse
                                       ↓
                              bush-sentiment → bush/flame/flare/pulse
                                            → bush/flame/bigjet/pulse
                                            → bush/pipeline/sentiment/result
                                                   ↓
                                             bush-tts   (espeak verse)
```

Backing services: `chromadb.service` (port 8000), `ollama.service` (port 11434, pre-existing).

## Deployment targets

Bush Glue runs on three different network topologies. MQTT broker discovery
behaves differently in each — read this before configuring services.

| Target | Broker location | How services find it |
|--------|----------------|----------------------|
| **WSL2 (dev)** | Windows host (Mosquitto) | `get_mqtt_broker()` reads `/proc/version`, detects WSL2, resolves the default gateway IP (e.g. `172.26.160.1`) |
| **ODROID / Pi on LAN** | Separate host on the same network | Auto-discovery does **not** work — the gateway IP is the router, not the broker. **Set `MQTT_BROKER` in each systemd service file.** |
| **Pi as broker** | `localhost` | `get_mqtt_broker()` falls back to `localhost` on native Linux — works correctly |

### Configuring the broker IP for ODROID / Pi deployments

In each `.service` file under `systemd/`:

```ini
[Service]
Environment=MQTT_BROKER=192.168.1.42   # IP of the machine running Mosquitto
```

`stt_t2v.py` also respects `MQTT_BROKER` from the environment — set it in your shell or
service file before starting `bush-stt`.

> **Note:** The `_windows_host_ip()` function in `stt_t2v.py` and `get_mqtt_broker()` in
> `bushutil.py` are both WSL2-specific. On any native Linux host where the broker is not
> at the default gateway address, always set `MQTT_BROKER` explicitly rather than relying
> on auto-detection.

---

## Prerequisites

### Windows side
- **Mosquitto MQTT broker** running on Windows, listening on all interfaces (port 1883).
  WSL2 reaches it at the default gateway IP (usually `172.26.160.1`).
  In `mosquitto.conf`:
  ```
  listener 1883 0.0.0.0
  allow_anonymous true
  ```

### WSL2 (Ubuntu)
- **Systemd enabled** — verify with `systemctl status`. If not enabled, add to `/etc/wsl.conf`:
  ```ini
  [boot]
  systemd=true
  ```
  Then restart WSL (`wsl --shutdown` from PowerShell).

- **WSLg** installed (ships with Windows 11 / recent Win10 builds). Verify:
  ```bash
  ls /mnt/wslg/PulseServer
  ```

- **Ollama** installed and running as a service with the embedding model loaded:
  ```bash
  ollama pull qwen3-embedding:0.6b
  systemctl status ollama
  ```

- **Rust / cargo** installed (for text-to-verse binary):
  ```bash
  curl https://sh.rustup.rs -sSf | sh
  ```

## Required Repositories

Clone these into `/mnt/c/Users/EB/` (paths are hardcoded in the service scripts):

| Repo | Purpose |
|------|---------|
| `FlamingBush/bushglue` | This repo — pipeline scripts |
| `FlamingBush/speech-to-text` | Vosk STT library + mic input |
| `FlamingBush/bbsentimentqq` | Emotion classifier HTTP + MQTT server |
| `FlamingBush/text-to-verse` | Rust binary (build separately) |
| `FlamingBush/t2v-chroma` | ChromaDB with verse embeddings |

```bash
cd /mnt/c/Users/EB
git clone https://github.com/FlamingBush/bushglue
git clone https://github.com/FlamingBush/speech-to-text
git clone https://github.com/FlamingBush/bbsentimentqq
git clone https://github.com/FlamingBush/text-to-verse
git clone https://github.com/FlamingBush/t2v-chroma
```

## System Packages

```bash
sudo apt-get update
sudo apt-get install -y espeak-ng python3-pip portaudio19-dev
```

## Python Dependencies

### System Python (used by bush-stt, bush-t2v, bush-tts, monitor)

```bash
pip3 install paho-mqtt rich numpy sounddevice vosk webrtcvad --break-system-packages
```

### bbsentimentqq venv

The sentiment service uses a pre-existing venv at `/home/ubuntu/bbsentimentqq-venv`
that has `transformers` and `torch` but no pip binary. Install paho directly:

```bash
pip3 install paho-mqtt \
  --target /home/ubuntu/bbsentimentqq-venv/lib/python3.12/site-packages/
```

If this venv doesn't exist yet, create it:

```bash
python3 -m venv /home/ubuntu/bbsentimentqq-venv
/home/ubuntu/bbsentimentqq-venv/bin/pip install transformers torch paho-mqtt
```

## Vosk Speech Model

Three model tiers — pick based on available RAM and acceptable latency:

| Model | Size | WER | Notes |
|---|---|---|---|
| `vosk-model-small-en-us-0.15` | 40 MB | ~15% | Minimum viable; use only if RAM-constrained |
| `vosk-model-en-us-0.22-lgraph` | 128 MB | ~9% | **Recommended** — best accuracy/RAM tradeoff |
| `vosk-model-en-us-0.22` | 1.8 GB | ~6% | Maximum accuracy; requires ~3 GB RAM peak |

The ODROID M2 (8 GB RAM) can comfortably run the lgraph model. Install it:

```bash
cd $STT_DIR   # e.g. /home/odroid/repos/speech-to-text
mkdir -p models
wget https://alphacephei.com/vosk/models/vosk-model-en-us-0.22-lgraph.zip
unzip vosk-model-en-us-0.22-lgraph.zip
mv vosk-model-en-us-0.22-lgraph models/en-us
```

To use the full model instead (best accuracy, higher RAM):

```bash
wget https://alphacephei.com/vosk/models/vosk-model-en-us-0.22.zip
unzip vosk-model-en-us-0.22.zip
mv vosk-model-en-us-0.22 models/en-us
```

## PulseAudio Noise Suppression

`module-echo-cancel` runs RNNoise-based suppression in the PA server before `stt-service`
ever sees the audio — effective against generator hum, fire roar, and crowd noise.

Add to `/etc/pulse/default.pa` (or load at runtime with `pactl load-module`):

```
load-module module-echo-cancel \
    use_master_source_name=1 \
    aec_method=webrtc \
    source_name=noise_suppressed \
    aec_args="noise_suppression=1 analog_gain_control=0 digital_gain_control=1"
```

Then point STT at the suppressed source:

```bash
# In systemd/odroid/bush-stt.service
Environment="STT_DEVICE=noise_suppressed"
```

Verify the source exists after reloading PA:

```bash
pactl list short sources | grep noise_suppressed
```

## text-to-verse Binary

```bash
cd /mnt/c/Users/EB/text-to-verse
cargo build --release
# binary lands at ~/.cargo/bin/text-to-verse
cargo install --path .
```

## t2v-chroma (ChromaDB + verse embeddings)

```bash
cd /mnt/c/Users/EB/t2v-chroma
python3 -m venv .venv
.venv/bin/pip install chromadb
# Populate the database — see that repo's README
```

## Shell Setup (Odroid)

Add to `~/.bashrc` so the `bush-*` utilities are on PATH and can find `bushutil`:

```bash
export PYTHONPATH=~/repos/bushglue
export PATH="$PATH:$HOME/repos/bushglue/utils"
```

Then reload: `source ~/.bashrc`

Available utilities:

| Command | Purpose |
|---------|---------|
| `bush-monitor` | Real-time pipeline TUI |
| `bush-pray` | Inject a phrase or WAV into the loopback |
| `bush-stt-file` | Replay a WAV file through the STT stage |
| `bush-integration-test` | End-to-end pipeline test |
| `bush-wait-for-http` | Poll a URL until it responds |

## Install systemd Units

```bash
sudo cp /mnt/c/Users/EB/bushglue/systemd/*.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now chromadb bush-t2v bush-sentiment bush-stt bush-tts
```

## Verify

```bash
# All services running
systemctl status chromadb bush-t2v bush-sentiment bush-stt bush-tts

# t2v HTTP healthy
curl http://localhost:8765/health

# Sentiment HTTP healthy
curl http://localhost:8585/

# Inject a transcript and watch it flow through
python3 -c "
import paho.mqtt.client as mqtt, json, time
c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
c.connect('172.26.160.1', 1883)
c.publish('bush/pipeline/stt/transcript', json.dumps({'text': 'what is the meaning of fire', 'ts': time.time()}))
c.disconnect()
"

# Watch logs
journalctl -u bush-t2v -u bush-sentiment -u bush-tts -f
```

## Monitor TUI

```bash
bush-monitor
```

Shows live STT transcripts, verses, emotion bars, ASCII bush (standby / flare /
bigjet / both), TTS status, and event log. Quit with Ctrl-C.

## Tuning / Replay

Feed the same input repeatedly to compare results as you adjust t2v config, affect
selection, ChromaDB collections, and other parameters.

### Level 1 — Full pipeline replay (audio → STT → MQTT)

Requires a mono, 16-bit PCM WAV file (16 kHz recommended):

```bash
bush-stt-file --file recording.wav --delay 3 --log runs/run1.jsonl
```

`--delay` pauses between utterances so you can watch `monitor.py` update in real time.
`--log` appends one JSONL line per utterance for side-by-side comparison across runs.

### Level 2 — Bypass STT (inject text directly)

```bash
# Single utterance
python3 -c "
import paho.mqtt.client as mqtt, json, time
c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
c.connect('172.26.160.1', 1883)
c.publish('bush/pipeline/stt/transcript', json.dumps({'text': 'what is the meaning of fire', 'ts': time.time()}))
c.disconnect()
"

# Feed a text file line-by-line (one utterance per line)
while IFS= read -r line; do
    python3 -c "
import paho.mqtt.client as mqtt, json, time
c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
c.connect('172.26.160.1', 1883)
c.publish('bush/pipeline/stt/transcript', json.dumps({'text': '$line', 'ts': time.time()}))
c.disconnect()
"
    sleep 3
done < prompts.txt
```

### Level 3 — Bypass STT + t2v (inject verse directly)

```bash
python3 -c "
import paho.mqtt.client as mqtt, json, time
c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
c.connect('172.26.160.1', 1883)
c.publish('bush/pipeline/t2v/verse', json.dumps({'verse': 'And the fire of the Lord fell', 'ts': time.time()}))
c.disconnect()
"
```

### Capturing pipeline output for comparison

```bash
mosquitto_sub -h 172.26.160.1 -t 'bush/pipeline/#' -v | tee runs/run1.log
```

Run the same input again after changing a parameter, save to `runs/run2.log`, then diff.

### Determinism notes

- **Vosk STT**, **Ollama embeddings**, **ChromaDB**, and **DistilBERT** are deterministic
  for identical inputs — same WAV file produces identical transcripts and verses.
- **`bbsentimentqq` fire patterns** use `random.random()` for jitter, so fire timing
  will vary between runs even with the same input.

## Fire Control TUI

```bash
python3 /mnt/c/Users/EB/bushglue/bush-firecontrol
# with a custom broker:
python3 /mnt/c/Users/EB/bushglue/bush-firecontrol --broker 192.168.86.50 --port 1883
```

Keyboard TUI for sending timed MQTT pulses directly to the flame relays (GP2 = flare,
GP3 = bigjet). Left half of QWERTY = bigjet, right half = flare; rows = short/medium/long
durations. ESC to quit.

## STT Accuracy Tuning

`stt-service` runs a four-stage accuracy pipeline controlled by env vars:

| Variable | Default | Effect |
|---|---|---|
| `STT_VAD_AGGRESSIVENESS` | `2` | webrtcvad aggressiveness 0–3 (3 = most aggressive noise filtering) |
| `STT_VAD_SILENCE_MS` | `810` | ms of consecutive silence before auto-finalize |
| `STT_CONFIDENCE` | `0.6` | Drop Vosk results below this mean word confidence (0.0–1.0) |
| `STT_LLM_CORRECT` | `0` | Set to `1` to enable Ollama post-correction |
| `STT_LLM_MODEL` | `qwen3:1.7b` | Ollama model used for post-correction |
| `OLLAMA_URL` | `http://localhost:11434` | Ollama base URL |

Set in the systemd service file under `[Service]`:

```ini
Environment=STT_VAD_AGGRESSIVENESS=3
Environment=STT_CONFIDENCE=0.65
Environment=STT_LLM_CORRECT=1
```

**LLM post-correction** adds ~200ms latency per utterance and requires `qwen3:1.7b`
to be loaded in Ollama. Pull it first:

```bash
ollama pull qwen3:1.7b
```

**Highpass filter** (200 Hz, gain +3 dB) is always active — implemented as a sox pipe
between the capture process and Vosk. Requires `sox` to be installed:

```bash
sudo apt-get install -y sox
```

## Troubleshooting

| Symptom | Check |
|---------|-------|
| `bush-t2v` fails to start | `chromadb.service` and `ollama.service` must be running first |
| `bush-sentiment` crashes on import | Run the paho venv install step above |
| No audio from TTS or sound effects | Verify `/mnt/wslg/PulseServer` exists; WSLg must be running |
| STT transcribes its own TTS output | `bush-tts` publishes `tts/done` when speech ends; `bush-stt` should mute automatically — check both are running |
| MQTT broker unreachable | Verify Mosquitto is running on Windows and `allow_anonymous true` is set |
| `text-to-verse` binary not found | Run `cargo install --path .` in the text-to-verse repo |

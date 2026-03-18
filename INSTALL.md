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
                                             bush-sound (synth audio)
                                             bush-tts   (espeak verse)
```

Backing services: `chromadb.service` (port 8000), `ollama.service` (port 11434, pre-existing).

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

### System Python (used by bush-stt, bush-t2v, bush-tts, bush-sound, monitor)

```bash
pip3 install paho-mqtt rich numpy sounddevice vosk --break-system-packages
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

```bash
cd /mnt/c/Users/EB/speech-to-text
mkdir -p models
wget https://alphacephei.com/vosk/models/vosk-model-en-us-0.22.zip
unzip vosk-model-en-us-0.22.zip
mv vosk-model-en-us-0.22 models/en-us
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

## Install systemd Units

```bash
sudo cp /mnt/c/Users/EB/bushglue/systemd/*.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now chromadb bush-t2v bush-sentiment bush-stt bush-tts bush-sound
```

## Verify

```bash
# All services running
systemctl status chromadb bush-t2v bush-sentiment bush-stt bush-tts bush-sound

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
journalctl -u bush-t2v -u bush-sentiment -u bush-tts -u bush-sound -f
```

## Monitor TUI

```bash
python3 /mnt/c/Users/EB/bushglue/scripts/monitor.py
```

Shows live STT transcripts, verses, emotion bars, ASCII bush (standby / flare /
bigjet / both), TTS status, and event log. Quit with Ctrl-C.

## Troubleshooting

| Symptom | Check |
|---------|-------|
| `bush-t2v` fails to start | `chromadb.service` and `ollama.service` must be running first |
| `bush-sentiment` crashes on import | Run the paho venv install step above |
| No audio from TTS or sound effects | Verify `/mnt/wslg/PulseServer` exists; WSLg must be running |
| STT transcribes its own TTS output | `bush-tts` publishes `tts/done` when speech ends; `bush-stt` should mute automatically — check both are running |
| MQTT broker unreachable | Verify Mosquitto is running on Windows and `allow_anonymous true` is set |
| `text-to-verse` binary not found | Run `cargo install --path .` in the text-to-verse repo |

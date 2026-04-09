# Bush Glue

Monorepo for the AI Am art installation pipeline. Modular system for critique of intersections of religion and technology.

## Structure

```
services/          Python microservices (uv workspace packages)
  stt/             Speech-to-text (Vosk)
  tts/             Text-to-speech (espeak-ng + sox)
  t2v-bridge/      Text-to-verse MQTT bridge (wraps Rust binary)
  sentiment/       Emotion classification + fire control (DistilBERT)
  sound/           Flame audio synthesis
  audio-agent/     Audio device discovery
  discord/         Discord /pray command bot
packages/          Shared Python packages
  bushutil/        MQTT broker detection, audio config, sox effects
t2v/               Rust text-to-verse server (Cargo project)
data/              Embedding database (Git LFS)
firmware/          CircuitPython relay controller (Pico 2 W)
systemd/odroid/    Systemd service files for ODROID deployment
utils/             CLI tools (monitor, firecontrol, integration test)
docs/              Architecture docs, MQTT topics reference
```

## Setup

```bash
# Install uv (once)
curl -LsSf https://astral.sh/uv/install.sh | sh

# Install all Python dependencies
uv sync --all-packages

# Build the Rust text-to-verse server
cd t2v && cargo build --release

# Extract chroma embedding database
cd data && tar xzf chroma.tar.gz
```

## Running services

Each service has an entry point in the venv:

```bash
.venv/bin/bush-stt
.venv/bin/bush-tts
.venv/bin/bush-t2v
.venv/bin/bush-sentiment
.venv/bin/bush-sound
.venv/bin/bush-audio-agent
.venv/bin/bush-discord
.venv/bin/chroma run --path data/chromadb
```

## Deploy to ODROID

```bash
git push origin main
ssh odroid-cmd 'cd ~/repos/bushglue && git pull && uv sync --all-packages'
ssh odroid-cmd 'sudo cp ~/repos/bushglue/systemd/odroid/*.service /etc/systemd/system/ && sudo systemctl daemon-reload'
ssh odroid-cmd 'sudo systemctl restart bush-stt bush-tts bush-t2v bush-sentiment bush-audio-agent'
```

## Detailed docs

See [docs/README.md](docs/README.md) for MQTT topics reference, message payloads, and architecture diagram.

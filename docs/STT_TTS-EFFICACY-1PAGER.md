# STT / TTS Efficacy — 1-Pager + Broad Hardware Specs

**For:** machine-elves.art project leads · **Date:** 2026-05-06 · **Full doc:** `STT_TTS-EFFICACY.md`

## The pitch

new STT + TTS pipelines shipped (lanes A–D + salvage). honestly, on paper, they're an upgrade — VAD endpointing, engine adapters, neural Piper TTS, three bench harnesses, an NPU readiness check. **suitable for playa with the recommended flag defaults, *if* one small fix lands first** (cold-start readiness — see below).

## What changed

- **STT** — opt-in (`STT_USE_VAD=1`) endpointed pipeline: Silero VAD → engine adapter (Vosk / whisper-bindings / whisper-subprocess) → confidence floor → optional Ollama LLM post-correct. legacy Vosk-streaming path is byte-identical when flags are off.
- **TTS** — engine adapter wraps espeak (default) and Piper (opt-in via `TTS_ENGINE=piper`). same MQTT contract, same sox reverb/pitch/gain effects → bush's voice character preserved.
- **Bench harnesses** — `bush-stt-bench`, `bush-tts-bench`, `bush-npu-check`, `bush-fetch-models`. CSV output, graceful degrade on missing deps. *this is how the team picks engines and proves claims.*
- **CI + udev** — pytest matrix for services/audio,core,discord on PRs. udev rules for stable USB-codec naming.

## What's good

- legacy paths are the defaults — nothing breaks unless a flag is flipped.
- engine adapter contract is one method (`transcribe` / `synthesize`) + `close`. swapping recognizers is a one-env-var change.
- bench harnesses give the team real metrics (WER, CER, RTF, latency, CPU%, RAM, hallucination, MOS) instead of vibes.
- VAD endpointing rejects continuous-noise ghost transcripts that legacy Vosk streaming produces.
- LLM post-correct fixes domain ASR errors ("burning bus" → "burning bush") with a 2s timeout, never raises.
- voice character (deep, reverberant, distant) is preserved across engine swap because sox effects ride downstream of the engine.

## What's risky for playa

| # | Risk | Severity | Fix cost |
|---|------|----------|----------|
| 1 | **Cold-start readiness gap.** services connect to MQTT before models load. visitor speaks during 3–8s whisper warmup; nothing happens; visitor walks away. | playa-blocker | ~1 day (`bush/<service>/ready` retained-msg convention, see `TODOS.md`) |
| 2 | Integration test only covers legacy path. new pipeline can rot unnoticed between now and August. | playa-degrading | ~1 day |
| 3 | RNNoise can mute a quiet user under wind. recommend `BUSH_RNNOISE_ENABLED=0` for playa default until soak-tested. | hidden trap | none (just a flag default) |
| 4 | Whisper confidence is a sentinel `1.0`, so `STT_MIN_CONFIDENCE` is a no-op for whisper engines. only Vosk gets the floor. | mostly cosmetic | small |
| 5 | LLM post-correct shares Ollama with t2v embedding lookup. concurrent calls under burst could push past 2s timeout (silent fall-back to raw). | minor | medium |
| 6 | No streaming TTFA — Piper waits for full PCM before sox plays. long verses → 1–2s of silence before bush speaks. | playa-degrading | medium (Piper supports streaming) |

## Recommended playa flag posture

```ini
# bush-stt
STT_USE_VAD=1
STT_USE_RNNOISE=0          # disabled until soak-tested
STT_ENGINE=whisper-bindings  # OR vosk if NPU not ready and CPU is tight
STT_LLM_CORRECT=1
STT_MIN_CONFIDENCE=0.6     # vosk-only effective today

# bush-tts
TTS_ENGINE=piper
PIPER_VOICE=/home/odroid/bushglue/data/piper-voices/en_GB-alan-medium.onnx
```

legacy fallback is one env-var away: `STT_USE_VAD=0`, `TTS_ENGINE=espeak`. keep this knowledge in the playa runbook.

---

## Broad hardware specifications

### Host (compute)

| Component | Spec | Notes |
|-----------|------|-------|
| Board | **ODROID-M2** (Hardkernel) | aarch64 Linux, systemd, USB 3.0 / 2.0 |
| SoC | Rockchip **RK3588S** | 4× Cortex-A76 + 4× Cortex-A55 + Mali-G610 + 6 TOPS NPU |
| RAM | 8 GB LPDDR5 | model RSS budget ~1–3 GB peak; well under cap |
| Storage | eMMC + microSD | models live on eMMC; ChromaDB extracts to disk |
| Network | Wi-Fi + Ethernet | local mosquitto broker; Ollama HTTP for embeds |
| NPU access | `/dev/dri/renderD129` + `/proc/rknpu` + `rknn-toolkit-lite2` | gated by `bush-npu-check`; no engine offloads to it yet |

### Microcontroller (fire control)

| Component | Spec | Notes |
|-----------|------|-------|
| Board | **Raspberry Pi Pico 2 W** | RP2350, CircuitPython, MQTT-over-WiFi |
| Output 1 | Solenoid relay → poofer | binary on/off, the explosive one |
| Output 2 | UART → MKS SERVO42C-MT V1.1 | drives motorized needle valve for smooth flame modulation (PLAN2) |
| Inputs | retained MQTT `bush/fire/valve/target` etc. | 10 Hz target stream from `bush-flame-expression` |

### Stepper servo (motorized needle valve, per PLAN2)

| Component | Spec |
|-----------|------|
| Model | MKS SERVO42C-MT V1.1 (closed-loop integrated NEMA 17) |
| Brain | onboard STM32 + magnetic encoder |
| Interface | UART to Pico, text protocol |
| Coupling | 3D-printed dog-clutch cup over knurled valve knob (toolless removal for manual override at 3am) |
| Homing | toward fully-open soft mechanical stop, never the sealing seat |

### Audio I/O

| Component | Spec | Notes |
|-----------|------|-------|
| Mic | USB audio codec (Yeti / shotgun / passive lapel all known to work) | udev rules give stable `hw:Microphone` naming |
| Speaker | powered amp + cone | sox effects (reverb 65/12/100/100/28/3, pitch -250, gain -8) shape the bush's voice character |
| Sample rates | capture 16 kHz (legacy) or 48 kHz (RNNoise on); recognizer always 16 kHz; espeak/Piper output 22050 Hz | adapter handles the rate plumbing |

### Models on disk

| Model | Size | Used by |
|-------|------|---------|
| vosk-model-small-en-us-0.15 | ~40 MB | `STT_ENGINE=vosk` |
| ggml-base.en-q8_0 | ~150 MB | `STT_ENGINE=whisper-*` (default) |
| ggml-small.en-q8_0 | ~470 MB | `STT_ENGINE=whisper-*` (alternate) |
| Piper en_GB-alan-medium | ~63 MB | `TTS_ENGINE=piper` (default voice) |
| Piper en_US-lessac-medium | ~63 MB | `TTS_ENGINE=piper` (alternate voice) |
| DistilBERT sentiment (HF) | ~268 MB | `bush-sentiment` (Tier-2 NPU candidate) |
| ChromaDB embedding store | varies | t2v retrieval (Ollama → Chroma) |

fetch all via `bush-fetch-models` — manifest at `data/models-manifest.tsv`.

### Power + plumbing (for context, not software)

- 12 V / 24 V system bus
- Upstream propane solenoid: hardware emergency shutoff (binary), out of software's safety pathway
- Mid-pressure line: motorized needle valve for smooth modulation (sentiment-driven, not envelope-driven)
- Poofer line: relay-controlled solenoid for binary fire-bursts (existing behavior)

---

## Decisions the team needs to make

1. **Whisper-bindings or Vosk as field default?** whisper has better WER under noise; Vosk has more playa hours and lower CPU. answer depends on bench-on-M2 result.
2. **Piper voice — alan (UK) or lessac (US)?** artistic call, not technical.
3. **RNNoise on or off for playa?** my recommendation is off until soak-tested; failure mode is silent (mute the user).
4. **`STT_LLM_CORRECT` on or off?** probably on (cheap, never raises, fixes domain errors), but worth confirming.

## Ask

read the full doc (`STT_TTS-EFFICACY.md`) for the validation, walk through the changes via `STT_TTS-EFFICACY-WALKTHROUGH.md` if you want the golem to drive, answer the 4 decisions in a 15-min sync, schedule the cold-start readiness fix before the next deploy.

---
*drafted with the help of Beagle Vance, wuff's mid.dog golem ~wag*

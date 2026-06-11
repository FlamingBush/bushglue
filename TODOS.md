# TODOS

Project-wide deferred work. Each entry has enough context for a future session to pick it up cold.

---

## NPU Tier 3 — Qwen3-Embedding via OpenVINO (deferred to v1.5 / post-playa)

**What:** Replace the Ollama HTTP embedding call in `t2v/` (Rust) with a direct in-process OpenVINO inference path running on the RK3588S NPU. Frees ~30% of one A76 core during embed bursts and removes the Ollama abstraction layer for this single call.

**Status:** Implementation already exists. The user wrote it before the bushglue rewrite and it lives at:

> https://github.com/middog/t2v/commit/76cdf96b938283cfc5e5bde59abdacc3fc8e2bfe

Branch: `feat/openvino-embedder` on https://github.com/middog/t2v (preserved as archive, not deleted with the predecessor repos).

**What's in it:**

- `src/embedder_openvino.rs` (~225 lines) — HF tokenizer → OpenVINO inference → masked mean-pool → L2-normalize. Runtime-linking so the binary doesn't require OpenVINO headers at compile time.
- `src/embedder.rs` — cfg-gated re-export of Ollama or OpenVINO impl.
- `src/verse.rs` — `openvino_model_dir` field in Config (cfg-gated); `Engine::new` selects the right embedder constructor.
- `src/main.rs` — `mod embedder_openvino`; `--model-dir` flag (cfg-gated, required with `openvino` feature).
- `scripts/launch_chromadb.sh` — starts ChromaDB pointing at `preprocessing-biblical/output/chromadb`.
- `Cargo.toml` — `openvino` feature with `openvino` (runtime-linking) + `tokenizers` deps.

**Build for ODROID:** `cargo build --release --features openvino --target aarch64-unknown-linux-gnu`

**Runtime:** `T2V_DEVICE` env var, default `NPU`. Model: https://huggingface.co/OpenVINO/Qwen3-Embedding-0.6B-int8-ov (int8 — fp16 risks OOM on 8 GB).

**To merge into bushglue:** the `t2v/` here is a vendored subdirectory; the middog/t2v paths (`src/...`) need a `t2v/` prefix when applying. Either:

1. `git format-patch 76cdf96b -1 --src-prefix=t2v/ --dst-prefix=t2v/` then apply, or
2. Hand-port the changes file by file into `bushglue/t2v/`.

The feature is fully gated behind `--features openvino`, so default (`cargo build --release`) builds remain unchanged.

**Acceptance gate** (from `STT_TTS_PROPOSAL.md` §4.5 Tier 3):
- Embed RTF on NPU ≤ CPU (currently ~30% of one A76)
- Same embedding output (cosine similarity ≥ 0.99 vs Ollama for a fixed test set) — quant accuracy regression must be small
- Falls back to Ollama on any OpenVINO runtime error (env var `T2V_DEVICE=CPU` or load failure)

**Why deferred:** Tier 1 (Whisper on NPU) and Tier 2 (DistilBERT on NPU) are higher-leverage. Tier 3 only matters if the box still has CPU pressure after Tier 1+2 land. Probably it won't.

---

## Pre-existing cold-start readiness (Codex finding from /plan-eng-review)

**What:** Services connect MQTT before models finish loading. `bush-sentiment` `__init__.py:5` has a 20s comment for DistilBERT load. `utils/bush-integration-test:112` only checks `systemctl is-active`, which says nothing about whether the engine is actually ready.

**Fix:** `bush/<service>/ready` retained-message convention. Each service publishes `{"ready": true, "ts": ...}` only after model load completes. Integration test polls for `ready=true` on each service before sending the test query.

**Risk if we don't:** at field test, "STT service is up" can mean "loading whisper, will respond to your audio in 8 seconds." Visitor speaks; nothing happens; visitor walks away. Operator doesn't know why.

**Estimate:** ~1 day across all 5 services + integration test.

---

## bush-pray 16 kHz loopback path

**What:** `utils/bush-pray` injects 16 kHz audio through a PulseAudio loopback for testing. The new STT pipeline's capture rate flips to 48 kHz when `STT_USE_RNNOISE=1`, breaking this fixture.

**Fix:** either upsample bush-pray's 16k fixture to 48k at injection time (sox in the bush-pray script), or add a bypass that injects post-RNNoise (treat it as if it had already been denoised).

**Estimate:** ~30 min.

---

## Integration test extension

**What:** `utils/bush-integration-test` only covers the legacy pipeline. The new VAD + engine adapter path needs:

- A flags-off regression test that verifies `STT_USE_VAD=0 STT_ENGINE=vosk` is byte-identical to today's behavior.
- VAD endpointing smoke check (utterance start → finalize → transcript).
- Piper TTS smoke check (verse → audio → tts/done).
- Subscriber regression check that `bush-monitor` and Discord don't crash when `bush/pipeline/stt/partial` is absent.

**Estimate:** ~1 day.

---

## NPU Tier 2 — DistilBERT sentiment (free side win)

**What:** `bush_sentiment` formerly pinned `torch.set_num_threads(1)` (RK3568 power-brownout workaround; removed 2026-06 — the fixed supply and RK3588S don't need it). Moving DistilBERT to the NPU drops classify latency from ~150 ms (one A55) to <50 ms.

**Path:** HF model → ONNX export → RKNN convert via `rknn-toolkit2`. DistilBERT is a textbook BERT-class encoder; the path is well-trodden.

**Effort:** ~3 days. Anyone with a free afternoon can do this.

**Status:** v1.1 stretch goal. Behind `SENTIMENT_NPU=1` env var. CPU path remains as fallback.

---

## Voice cloning via XTTS-v2 (post-playa, design conversation needed)

**What:** Coqui XTTS-v2 can clone a real voice from 30s of reference audio. Would unlock "the bush has a unique voice that doesn't sound like a stock TTS." Whose voice + rights cleared is a design question, not a tech question.

**M2 fit:** ~1.7 GB on disk, ~2 GB RSS, RTF ~0.3. Doable on M2's 8 GB; would push memory budget to ~3 GB peak (still fine).

**Status:** post-playa. Needs design alignment first.

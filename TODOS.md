# TODOS

Project-wide deferred work, tiered by priority anchored to the Burning Man field deploy (late August 2026, ~3.5 months from 2026-05-07).

Reconciled by `/autoplan` 2026-05-07. Replaces the original 6-item list with the merged finding-set from `docs/STT_TTS-ENG-REVIEW.md` (14 findings, 10 original + 4 amended) plus `/autoplan` dual-voice surfacings. Sequencing follows `docs/STT_TTS-ENG-REVIEW.md:393-431` (Tier 0/1/2/3) for the items that overlap. Full review trail: `docs/AUTOPLAN-REVIEW-2026-05-07.md`.

---

## P0 — Ship blockers (must be done before pre-playa freeze)

### 1. Pico safety: force-OFF before blocking states

**Reason:** propane safety. `firmware/relay-control/CIRCUITPY/code.py:232,248,465,525` — `mqtt_open()` blocks up to 5s during reconnect/scan; an active flare pulse can keep a solenoid open for the full 5-second window. Eng review §2.3 + dual voice consensus.

**Fix:** add `force_all_solenoids_off()` (sets pins low, clears all `off_ms_*` deadlines). Call before EVERY blocking path (`wifi_connect`, `mqtt_open`, `tcp_probe`, scan verify). Publish `bush/fire/safety/forced_off` event so operator sees when this fires. Truncated pulses are acceptable fail-safe behavior.

**Effort:** human ~2 hours / CC ~30 min + Pico USB-unplug deploy cycle.

### 2. MQTT contract upgrade (status enum + LWT + version + fault topic)

**Reason:** the cold-start gap and the silent-failure gaps share a root cause: services have no way to communicate health state. Both Codex and the independent Claude subagent agreed (consensus 6/6) that the original `bush/<service>/ready` boolean is underspecified.

**Fix:**
- Add `bushutil.MqttServiceClient` wrapper: `will_set` LWT, `publish_status(offline|starting|ready)` retained, `publish_version(git-hash)` retained, `publish_fault({error,ts,context})` non-retained. ~80 lines.
- Each of 5 services migrates to it. Subscribe inside `on_connect` (not main thread).
- `ready_latched` flag preserves status across reconnects.
- Update `docs/mqtt-architecture.dot` in same commit.

**Effort:** human ~1.5 days / CC ~1 hour + 5 deploy cycles.

### 3. Sentiment self-heal + classifier API verify

**Reason:** combined fix. (A) Eng review §2.1 — `loop_forever()` exit kills daemon thread silently; HTTP /:8585 keeps responding so systemd never restarts. (B) Eng review §9.A — `return_all_scores=True` may already be raising `TypeError` inside swallowing try/except; could explain "sentiment never fires."

**Fix:**
- §9.A: 30-min test in locked uv environment. If shape shifted, one-line fix (`top_k=None` or unwrap).
- §2.1: load model first; MQTT in main thread. Or `connect_async() + reconnect_delay_set() + loop_start() + on_connect/on_disconnect`. No wrapper thread that vanishes.
- Audit same pattern across sound, audio-agent, t2v, tts (sentiment is the outlier with separate HTTP server).

**Effort:** human ~1 day / CC ~1 hour + 1-2 deploy cycles.

### 4. t2v silent-failure mitigation + stock fallback verse

**Reason:** Eng review §2.9 + Codex CEO finding 7. t2v can timeout on Ollama/Chroma calls and publish nothing → bush silent → visitor walks away. Codex specifically suggests `STOCK_FALLBACK_VERSE` as strategically stronger than more accelerator work.

**Fix:**
- Wrap Ollama embed + Chroma query in timeout (suggest 3-5s).
- On timeout: publish `bush/<svc>/fault` + a stock fallback verse from a small in-process list.
- Document the fallback verse list in operator runbook so the operator knows when bush is in fallback mode.

**Effort:** human ~½ day / CC ~30 min.

### 5. bush-pray loopback fix (mode-aware playback pipe)

**Reason:** the new STT pipeline's capture rate flips to 48 kHz when `STT_USE_RNNOISE=1`, breaking the 16 kHz fixture. Codex eng#6 caught a subtle bug in the original plan: synthesis-side upsample leaves cached fixtures still 16k; bypass-RNNoise stops testing real input.

**Fix:** keep one black-box harness; upsample in the playback pipe based on active STT mode. Force mono 48kHz s16le for both generated and cached WAVs.

**Effort:** human ~1 hour / CC ~15 min.

### 6. Cold-start integration test extension (with byte-identical regression seam)

**Reason:** Eng review §2 + §6. New VAD/RNNoise/whisper pipeline never end-to-end tested; legacy "byte-identical when flags off" promise unverified.

**Fix:**
- `utils/bush-integration-test`: wait for `status=ready` retained for all 5 services before audio inject. Fix SUBACK race (`connected.set()` after `on_subscribe` for all topic mids). Add VAD endpointing smoke. Add Piper TTS smoke. Add NPU pre-check (only on aarch64 + `bush-npu-check` exit 0). Add subscriber regression check that bush-monitor and Discord don't crash when `bush/pipeline/stt/partial` is absent.
- Extract `_run_legacy_iteration` behind fake-recognizer + fake-MQTT seam. Pin Vosk model SHA256, fixed WAV fixture (NOT espeak-synth), expected JSON byte-for-byte. Run in CI.

**Effort:** human ~1.5 days / CC ~1.5 hours + multiple deploys.

### 7. bushutil.get_mqtt_broker() subprocess timeout

**Reason:** Eng review §2.5. `subprocess(['ip', 'route'])` with no timeout. Low likelihood of hang but every service blocks at startup if the WSL2 dev path hits this.

**Fix:** `subprocess.run(..., timeout=2)` with `try/except TimeoutExpired: return "localhost"` fallback. One-line change, 3-line test.

**Effort:** human ~15 min.

### 8. Pre-playa freeze + boring-default fallback config

**Reason:** Codex CEO finding 7 — strategic alternative dismissed too quickly. The Whisper-bindings + Piper config is impressive but Vosk + espeak has the deeper field hours. For a one-shot deploy, boring survives.

**Fix:**
- Add `/etc/systemd/system/bush-*.service.d/playa.conf` files committed to repo with the documented playa flag posture (per `STT_TTS-EFFICACY.md:20-35`).
- Also commit `playa-boring.conf` as a one-command rollback to Vosk + espeak.
- Add `bush-rollback` script in `utils/` that flips every service back to legacy config.
- Set freeze date: 7 days before truck-roll. No code changes after that, only flag overrides.

**Effort:** human ~3 hours / CC ~30 min.

### 9. Operator runbook (1-page PDF)

**Reason:** both dual voices flagged this. Without it, the new fault/status topics are signals nobody reads. Codex: "Burning Man does not grade on architecture purity; it grades on whether a sleep-deprived volunteer can recover the piece at 3am."

**Fix:** 1-page PDF that maps fault topic patterns to `systemctl restart` commands. Print, laminate, ziptie to the trailer interior. Single source of truth for operator decision-making at 3am.

**Effort:** human ~3 hours / CC ~45 min draft + iterate.

---

## P1 — Playa readiness (do if calendar allows)

### 10. LLM post-correct: prompt-injection mitigation + adversarial eval

**Reason:** subagent finding F6 + Codex eng#7. `services/audio/src/bush_stt/postprocess.py:27,40,55` — raw transcript interpolated into prompt template. Visitor whispering "ignore previous instructions" can make bush speak attacker-controlled text. Reputational risk.

**Fix:** wrap user transcript in delimiters + JSON; add post-LLM length + regex whitelist clamp; add 50-prompt adversarial corpus eval (10 hostile / 20 neutral / 20 in-domain). TTS-side profanity gate (catches anything that slips through).

**Effort:** human ~1 day / CC ~1.5 hours.

### 11. Discord alert on fault topics

**Reason:** Discord bot already exists; subscribing to `bush/<svc>/fault` and posting to a private channel is ~1 hour of work that gives operators async visibility.

**Fix:** add subscription to `bush_discord/__init__.py`. Format messages with timestamp + service + error excerpt.

**Effort:** human ~1 hour / CC ~15 min.

### 12. bush-monitor valve subscription fix

**Reason:** Eng review #13. `bush-monitor` docs claim valve subscriber that doesn't exist; only `bush-valve watch` actually subscribes.

**Fix:** add `bush/fire/valve/*` subscriptions to bush-monitor's TOPICS list. Either implement or update docs to reality. (Implementing is cheaper.)

**Effort:** human ~30 min.

### 13. M2 bench: thermal soak + memory budget

**Reason:** subagent finding F9 + Codex CEO finding 5. `torch.set_num_threads(1)` was M1 brownout mitigation; removing on M2 without proving brownout-free under sustained load is a bet.

**Fix:** 1-hour bench at ~30°C ambient + heated enclosure + continuous load. Capture RTF stability per minute, memory residency (`pmap`), thermal throttle events. Save to `~/.gstack/projects/FlamingBush-bushglue/bench-2026-XX-soak.json`.

**Effort:** human ~2 hours.

### 14. Audio recording / PII / consent surface

**Reason:** subagent finding F7. `bush-discord` records visitor speech via Discord voice-recv + saves attachments to WAVS_DIR. No retention policy, no signage, no token mode-0600 verification.

**Fix:**
- WAVS_DIR rotation: 24h tmpfs OR cron purge.
- Verify `bush-discord.service` uses `EnvironmentFile=` for token, mode 0600.
- "We are recording" sign spec in operator runbook (placement + content).

**Effort:** human ~2 hours.

### 15. TTS payload asymmetry hardening

**Reason:** Eng review #6 (amended). `speaking` carries `{text, ts}`, `done` carries `{ts}` only. Footgun for stricter `payload["text"]` code.

**Fix:** make `done` also carry `text` (or document the asymmetry in MQTT topics doc). One-line fix; subscribers tolerate either way.

**Effort:** human ~15 min.

### 16. Doc/firmware drift fixes (valve `moving` + monitor topic list)

**Reason:** Eng review #12, #13. Docs claim `moving` valve state firmware never emits (firmware actually emits `initializing`).

**Fix:** docs match firmware. Audit and align.

**Effort:** human ~30 min.

### 17. t2v wrapper stderr drain

**Reason:** Eng review #14. t2v wrapper pipes `stderr` but never drains. If Rust child writes enough stderr, it blocks.

**Fix:** `stderr=None` (let journald capture) or add a drain thread.

**Effort:** human ~15 min.

---

## P2 — Stretch / post-playa

### 18. NPU Tier 1 — Whisper on RKNN

**Reason:** `STT_TTS-EFFICACY.md:244` — "highest leverage; not yet implemented." Path: HF ggml-base.en → ONNX → RKNN via `rknn-toolkit2`. Acceptance: same WER as A76 whisper, lower CPU, frees ~3 cores during busy moments. **Pre-playa stretch lift candidate** if verification calendar gives slack.

**Effort:** human ~3-5 days. Calendar risk dominates the technical risk.

### 19. NPU Tier 2 — DistilBERT sentiment on RKNN

**Reason:** v1.1 stretch goal per existing TODOS. Subagent eng F8 caught a hidden complexity: int8 quant can shift argmax label on borderline inputs. Need `match rate ≥ 0.97 on N=200 verses` gate before flipping `SENTIMENT_NPU=1`.

**Effort:** human ~3 days.

### 20. NPU Tier 3 — Qwen3 embedding via OpenVINO (in t2v)

**Reason:** implementation already exists at `middog/t2v` commit `76cdf96b`. v1.5 / post-playa per existing TODOS. Confirmed unchanged.

**Build for ODROID:** `cargo build --release --features openvino --target aarch64-unknown-linux-gnu`
**Runtime:** `T2V_DEVICE` env var, default `NPU`. Model: https://huggingface.co/OpenVINO/Qwen3-Embedding-0.6B-int8-ov (int8 — fp16 risks OOM on 8 GB).
**Acceptance gate** (from `STT_TTS_PROPOSAL.md` §4.5 Tier 3):
- Embed RTF on NPU ≤ CPU (currently ~30% of one A76)
- Same embedding output (cosine similarity ≥ 0.99 vs Ollama for a fixed test set)
- Falls back to Ollama on any OpenVINO runtime error

**Effort:** ~1 day to merge once needed.

### 21. Structured logging migration

**Reason:** P2 polish. JSON-per-line logs make journalctl + jq + grep work cleanly for volunteer debugging.

**Effort:** human ~1 day.

### 22. M2 fit / memory budget for voice cloning (if pursued post-playa)

**Reason:** Coqui XTTS-v2 ~1.7 GB on disk, ~2 GB RSS, RTF ~0.3. Doable on M2's 8 GB but pushes peak to ~3 GB. Confirm headroom before committing.

**Status:** post-playa, depends on outcome of design conversation tracked in IDEAS.md (see "Out of TODOS" below).

---

## Out of TODOS (per /autoplan recommendations)

- **Voice cloning identity / persona / rights conversation** — art-direction question, not engineering. Tracked in `IDEAS.md` or `/office-hours` brainstorm. The bush sounding distinct enough is already 5/5 in efficacy review (`STT_TTS-EFFICACY.md:232`). The XTTS-v2 *engineering* (#22 above) is contingent on this design conversation, not the other way around.
- **Grafana / Prometheus stack** — overkill for a single box. Two `tmux` panes + journalctl is enough.
- **MQTT v5 migration** — mosquitto-only deploy; v3 is fine.
- **Custom paho-mqtt fork** — stock paho is enough.
- **Bus-factor mitigation via dev pairing** — at this scale, the operator runbook (P0 #9) is the bus-factor mitigation that matters; deeper handoff is post-playa.

---

## Calendar reconciliation

Approximate effort budget:
- **P0 (9 items):** human ~6-8 days / CC+gstack ~6-8 hours of engineering + ~3-4 days of verification deploys
- **P1 (8 items):** human ~4-5 days / CC+gstack ~3-4 hours + ~2 days verification
- **P2 (5 items):** post-playa unless P0+P1 finish with weeks to spare

3.5 months from 2026-05-07 → late August 2026. P0+P1 fits comfortably. NPU Tier 1 (P2#18) might fit if motorized-needle-valve work (PLAN2.md) wraps efficiently. Pre-playa freeze 7 days before truck-roll → freeze date approximately 2026-08-15 (assuming late-Aug Burning Man).

---

*Original 6-item TODOS preserved at `~/.gstack/projects/FlamingBush-bushglue/main-autoplan-restore-20260507-100310.md`. Full /autoplan review trail (CEO + Eng phases, dual voices, decision audit trail): `docs/AUTOPLAN-REVIEW-2026-05-07.md`.*

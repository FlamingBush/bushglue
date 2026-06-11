# STT / TTS Efficacy & Playa Suitability Review

**Author:** wuff (with Beagle Vance, their mid.dog golem)
**Date:** 2026-05-06
**Audience:** marcus + machine-elves.art crew + anyone touching `bushglue/services/audio` or `bushglue/services/core`
**Status:** DRAFT — read for review, no behavior changes proposed here. all the new pipelines already shipped in lanes A–D + the salvage commit (5ec304f). this doc just answers "did we do the right thing, and is the thing we did good enough for playa?"

---

## TL;DR

honestly the new STT and TTS pipelines are in good shape — the engineering taste behind them is excellent, the legacy paths are byte-identical when flags are off, and the bench harnesses (`bush-stt-bench`, `bush-tts-bench`, `bush-npu-check`) give the team a way to *prove* claims rather than guess. the work is suitable for Burning Man at the software layer with three caveats:

1. the **cold-start readiness gap** (services connect to MQTT before models load) is the single highest-risk unfinished item — if a visitor speaks during the 3–8 second whisper warmup window, nothing happens and they walk away. fix is small (`bush/<service>/ready` retained-msg convention), already documented in TODOS.md, but not yet implemented.
2. **integration test only covers the legacy path** — there's no smoke test that proves `STT_USE_VAD=1 STT_ENGINE=whisper-bindings TTS_ENGINE=piper` end-to-ends, which means the new pipeline could rot unnoticed between now and August.
3. **NPU is checked but not used** — `bush-npu-check` confirms the RK3588S NPU is reachable, but no engine actually offloads to it yet. that's a future-leverage gap, not a playa-blocker.

everything else is polish or post-playa work.

the recommended **default flag posture for playa** is:

```ini
# /etc/systemd/system/bush-stt.service.d/override.conf
Environment=STT_USE_VAD=1
Environment=STT_USE_RNNOISE=0          # disabled until soak-tested at temperature
Environment=STT_ENGINE=whisper-bindings
Environment=STT_LLM_CORRECT=1
Environment=STT_MIN_CONFIDENCE=0.6     # vosk-only effective; whisper engines return 1.0 sentinel

# /etc/systemd/system/bush-tts.service.d/override.conf
Environment=TTS_ENGINE=piper
Environment=PIPER_VOICE=/home/odroid/bushglue/data/piper-voices/en_GB-alan-medium.onnx
```

and keep the legacy fallback one env-var away (`STT_USE_VAD=0`, `TTS_ENGINE=espeak`) because that's the path with the most playa hours behind it. ~wag

---

## 1. What changed (concrete)

### 1.1 STT: from "always-on Vosk streaming" to "VAD-endpointed engine adapter"

**Original** (still the default — preserved as the legacy path inside `services/audio/src/bush_stt/`):

```
parec/arecord 16k mono int16 LE
    → SpeechToText.accept_audio()       (Vosk KaldiRecognizer, streaming)
    → MQTT bush/pipeline/stt/partial   (per-chunk partial)
    → MQTT bush/pipeline/stt/transcript (on AcceptWaveform-true)
```

continuous transcription. Vosk is famously chatty under noise — whatever PCM you feed it, it returns *some* word lattice. silence frames produce empty partials but ambient hum can produce ghost transcripts.

**New pipeline** (opt-in via `STT_USE_VAD=1`):

```
parec/arecord 48k OR 16k mono int16 LE
    → [optional] RnnoiseFilter         (480-sample frames @ 48k)
    → [optional] soxr resample 48 → 16
    → VadEndpointer                    (Silero VAD, locked D3 params)
    → STTEngine.transcribe(utterance)  (Vosk | whisper-bindings | whisper-subprocess)
    → confidence floor (drops if < STT_MIN_CONFIDENCE)
    → [optional] correct_transcript()  (Ollama qwen3:0.6b, 2s timeout, never raises)
    → MQTT bush/pipeline/stt/transcript {"text", "confidence", "ts"}
```

**Locked VAD parameters** (env-overridable via `BUSH_VAD_*`, but defaults are eng-review-D3 locked):

| Param | Default | Why |
|---|---|---|
| `min_silence_ms` | 600 | 600ms of trailing silence ends an utterance — long enough to swallow inter-phrase pauses, short enough to feel responsive |
| `max_utterance_ms` | 15000 | hard force-cut at 15s; the bush isn't trying to transcribe sermons |
| `pre_roll_ms` | 200 | seed utterance with 200ms before voice trigger; covers most phoneme onsets |
| `post_roll_ms` | 300 | trailing silence carried into utterance for engine context |
| `min_utterance_ms` | 250 | utterances shorter than 250ms of *voiced* audio (not buffer length) are discarded as noise |
| `threshold` | 0.5 | Silero voice probability threshold |

**Engine adapter contract** (`services/audio/src/bush_stt/engines/base.py`): one method `transcribe(audio_pcm) → {text, confidence, ts}`, one `close()`. three engines ship:

| Engine | Lib | Confidence semantics | Typical RTF (M2 A76) |
|---|---|---|---|
| `vosk` | Vosk + KaldiRecognizer | mean of word-level `conf` field, real values | ~0.10 (estimated, run bench) |
| `whisper-bindings` | pywhispercpp | sentinel `1.0` if any text else `0.0` | ~0.3–0.5 base.en, ~0.7–1.0 small.en |
| `whisper-subprocess` | whisper-cli binary | sentinel `1.0` if any text else `0.0` | ~0.4–0.6 base.en (extra fork cost) |

confidence semantics matter — see §3.4 (risks).

**Mute coordination** (preserved across both paths, with VAD-aware extensions on the new path):

- `bush/pipeline/tts/speaking` → mute STT, drop in-flight VAD utterance, ALSA pause if STT/TTS share a card.
- `bush/pipeline/tts/done` → unmute, full VAD reset, ALSA resume.
- 30s safety timeout if `done` never arrives.

**Force-finalize semantics changed.** Legacy path harvests the current Vosk partial; new path emits a canned phrase from `FALLBACK_PHRASES` (15 verses-of-the-bush prompts: "what is the fire", "speak of the light", etc.) and drops VAD state. simpler, predictable, never empty.

### 1.2 TTS: from "espeak | sox" subprocess pipe to engine adapter

**Original** (preserved as default — just wrapped, not changed):

```
text → espeak-ng --stdout (WAV)
     → sox -t wav - <output_device> gain -8 pitch -250 reverb 65 12 100 100 28 3
```

inline subprocess pipe in the bush-tts main loop. one process group per verse.

**New pipeline** (default behavior unchanged; `TTS_ENGINE=piper` opts into neural):

```
text → TTSEngine.synthesize(text)         (espeak | piper)
     → returns {audio_pcm, sample_rate, ts}
     → sox -t raw -r <sr> ... <output_device> <build_sox_effects(clarity)>
```

the engine returns raw int16 LE PCM and its native sample rate; `_sox_cmd(sample_rate)` builds the sox invocation with the engine's rate (espeak: 22050, Piper en_GB-alan-medium: 22050, en_US-lessac-medium: 22050 — note Piper voices vary, the adapter reads the `.onnx.json` sidecar to get the real rate). downstream sox effects unchanged: clarity-driven `build_sox_effects` from `bushutil`. **the bush's voice character — deep, reverberant, distant — is preserved across engine swap.**

| Engine | Lib | Voice quality | Typical RTF (M2 A76) | RAM peak |
|---|---|---|---|---|
| `espeak` | espeak-ng subprocess | classic robotic, intelligible | <0.05 | tiny |
| `piper` | piper subprocess (ONNX) | naturalistic neural | ~0.1–0.3 (medium voice) | ~200–300 MB |

**Discord** (`services/discord/`) was migrated to the same engine adapter so the /pray bot can also use Piper if configured.

### 1.3 Bench harnesses (the single most important addition)

three new utilities under `utils/`:

- **`bush-stt-bench`** — drives any registered STT engine against a labelled corpus (per-utterance WAV + transcripts.tsv). emits CSV with: WER, CER, RTF, latency, peak CPU%, peak RAM, hallucination flag (engine produced text on a silence-only clip). graceful degrade: missing `jiwer` / `psutil` / `soxr` → sentinel `-1` in the affected columns + one-line install hint. **this is how the team picks the recognizer.**

- **`bush-tts-bench`** — same shape for TTS: TTFA, total latency, RTF, CPU%, RAM, optional MOS prompt (1–5, plays the wav via aplay then asks). aggregates child-process CPU so subprocess engines (espeak, piper) report meaningful numbers, not 0%.

- **`bush-npu-check`** — 5-stage RK3588 NPU readiness gate: platform detect → /dev/dri render nodes → /proc/rknpu kernel driver → rknn-toolkit-lite2 import → canned mobilenet_v2.rknn inference. exit codes are deterministic (0 pass / 1 fail / 2 not-on-rk3588). this is the gate for any future RKNN offload work — if it doesn't pass, NPU work is deferred.

- **`bush-fetch-models`** — manifest-driven model fetcher (`data/models-manifest.tsv`), idempotent with sha256 verification, supports raw + .zip + .tar.gz. ships sentinels `TBD` for hashes pending first download. removes the "git-LFS the models or download by hand each time" footgun.

a default TTS bench corpus ships at `data/tts-bench-corpus.tsv` (9 utterances spanning short / medium / long / biblical / punctuation / numbers).

### 1.4 CI + udev (salvaged from earlier work)

- `.github/workflows/ci.yml` — pytest matrix across `services/audio`, `services/core`, `services/discord`. tests exist (see `services/audio/tests/test_*.py` for VAD, denoise, engines, postprocess, main loop) but **only run in CI on x86 ubuntu** — they don't catch ARM-specific failures.
- `udev/` — USB-codec rules for stable mic device naming (decouples ALSA `hw:Microphone` from hotplug order).
- Salvage commit `5ec304f` also includes the confidence gate + LLM post-correct that landed in the recently merged main loop rewrite.

---

## 2. Efficacy: did we make the right things better?

### 2.1 What the new STT pipeline buys us (on paper)

> *honestly, on paper, a lot. the playa-day measurements are still TBD until someone runs the bench against a real-noise corpus, but the mechanisms are sound.*

- **VAD endpointing replaces continuous transcription.** legacy Vosk streaming will produce ghost text on ambient noise — wind, generator hum, distant crowd. Silero VAD + min-utterance-of-voiced-time gating should reject these. measured outcome to confirm: hallucination count on silence clips in `bush-stt-bench` should drop from "varies" with legacy to ~0 with new.
- **RNNoise reduces broadband hiss before the recognizer sees it.** good for steady background; less helpful for transient noise (fire whoosh, distant booms, art-car bass thumps). the chain is: 48k capture → RNNoise → 48→16 soxr → VAD → engine. RNNoise is causal so it adds ~10ms of latency.
- **Confidence floor protects t2v.** the t2v retrieval will return a verse for *any* string, so a low-confidence transcript like "burning bus the wind blew" can pull a misaligned verse and the bush says something nonsensical. dropping `confidence < 0.6` (Vosk only — see §3.4) cuts that failure mode.
- **LLM post-correct fixes domain ASR errors.** "burning bus" → "burning bush", "moses cease" → "Moses". prompt is locked, temperature 0.0, 2s timeout, never raises. defense-in-depth length checks reject runaway corrections.
- **Engine adapter preserves shipping path.** if Whisper turns out to be too slow on M2 A76 cores under load, swap back to Vosk via `STT_ENGINE=vosk`. one env var. no code change.

### 2.2 What the new TTS pipeline buys us (on paper)

- **Piper sounds human-shaped.** espeak is intelligible but robotic; Piper en_GB-alan-medium is naturalistic. the bush's reverb/pitch/gain effects ride on top, so the *character* (deep, distant, oracular) is preserved across the engine swap. think "old man's voice from across the cliff" instead of "synthesizer's voice from across the cliff."
- **Engine adapter preserves shipping path.** same env-var swap argument as STT.
- **Same MQTT contract.** `bush/pipeline/tts/speaking` and `bush/pipeline/tts/done` semantics unchanged; the variable-valves service, sentiment fire loop, and STT mute gate all keep working.

### 2.3 What the new pipelines do NOT buy us

honestly worth being explicit:

- **the new STT path is not faster than legacy in latency-of-first-feedback.** Vosk streaming gives you partials within ~200ms; the new path gives you nothing until the VAD endpoints (i.e., you stopped speaking + 600ms silence). this is a feature, not a bug — the bush isn't a real-time captioning system, it's a turn-taking conversation partner — but if anyone is expecting the legacy "instant partials" UX, they'll be surprised.
- **whisper engines have no real confidence.** their confidence floor is a no-op (always 1.0 if any text). only Vosk gets the confidence-floor protection in practice.
- **no streaming TTFA gain yet.** Piper subprocess is still synchronous — bush waits for the full PCM before sox starts playing it. on long verses (~5s+ audio) this adds noticeable lag before the first word. a streaming Piper engine would help; not implemented.
- **NPU is unused.** all inference is on A76 CPU cores. the 6 TOPS NPU sits idle. (see §4 for the deferred tier-1/2/3 ladder.)

---

## 3. Suitability for Burning Man at the software layer

playa is a hostile testbed: dust, heat, intermittent power, volunteers fixing things at 3am with no internet. the software has to be (a) reliable, (b) graceful in failure, and (c) understandable to someone who didn't write it. how does the new work hold up?

### 3.1 Reliability — what helps

- **Default-off behavior.** `STT_USE_VAD=0` and `TTS_ENGINE=espeak` are the defaults. unless someone flips a flag, the playa-tested legacy paths run. **this is the right risk posture.** the default is the boring known-good; the new stuff is opt-in.
- **Graceful degrade in benches.** `jiwer`, `psutil`, `soxr` missing → sentinel `-1` + install hint. someone bringing up a fresh laptop on playa to run a quick bench won't be blocked by missing pip packages.
- **Subprocess engines are crash-isolated.** if Piper segfaults on weird input, the engine throws and the bush-tts main loop publishes `done` and skips the verse. the pipeline doesn't deadlock.
- **VAD `drop_in_flight` on `tts/speaking`.** when the bush starts speaking, any partial utterance the VAD was collecting gets discarded. this prevents the bush from transcribing its own speech if mic+speaker share a room.
- **30s safety timeout on mute.** if TTS hangs and never publishes `done`, mic returns. dead-air upper bound is bounded.
- **Confidence gate + LLM correct have explicit failure modes.** post-correct's `urlopen` is wrapped in a 2s timeout and a never-raises wrapper. if Ollama is dead, the raw transcript ships unchanged. this is the right behavior.

### 3.2 Reliability — what hurts (the gaps)

> *these are the things that, if a volunteer is reading this at 3am with the bush silent, will be the actual reason.*

| # | Gap | Severity | Fix complexity |
|---|---|---|---|
| 1 | **Cold-start readiness gap.** services connect to MQTT before models load. whisper bindings warmup is ~3–8s (model load + JIT). visitor speaks during that window; nothing happens. integration test reports green because `systemctl is-active` is true. | playa-blocker | small (~1 day across all 5 services + integration test) |
| 2 | **Integration test doesn't cover new path.** flags-off regression, VAD smoke, Piper smoke, NPU pre-check, "no partial topic" subscriber regression — none exist. | playa-degrading | small (~1 day) |
| 3 | **bush-pray fixture broken when `STT_USE_RNNOISE=1`.** capture rate flips to 48k and the bush-pray loopback fixture is 16k. test injection silently fails. | annoying | tiny (~30min) |
| 4 | **RNNoise can mute the user.** if RNNoise mis-models a quiet voice as noise (especially in a windy environment with low SNR), VAD never triggers and the bush appears deaf. **recommend `BUSH_RNNOISE_ENABLED=0` for playa default** until soak-tested at temperature. the engine adapter contract still gives us 90% of the win without RNNoise (the VAD endpointing is the bigger benefit). | hidden trap | none — just a flag default |
| 5 | **Whisper confidence is fake.** `STT_MIN_CONFIDENCE=0.6` is a no-op for whisper engines (always 1.0 if any text). this is fine if you're using Vosk; but if the field default is whisper-bindings, the gate isn't gating anything. | mostly cosmetic | small (compute log-prob from per-segment confidence; `pywhispercpp` exposes it via segment fields) |
| 6 | **LLM post-correct shares Ollama with t2v.** Ollama is also doing embedding lookup for verse retrieval. concurrent calls during a burst could push latency past the 2s timeout, and post-correct silently falls back to raw text. degrades the "burning bus → burning bush" UX silently. | minor | medium (separate Ollama instance, or run the correction model out-of-process with a queue) |
| 7 | **No streaming Piper.** TTFA = total synthesis time. on a long verse the bush stares silently for 1–2 seconds before speaking. visitors interpret as "didn't hear me." | playa-degrading | medium (Piper has a streaming output mode; engine adapter contract supports TTFA but neither implementation streams) |
| 8 | **TTS clarity tunable but undocumented in volunteer-facing docs.** the `bush/audio/tts/set-clarity` topic exists; the playa runbook doesn't mention it. operators won't know to dial intelligibility up at 3am when wind is gusting. | minor doc gap | tiny |

### 3.3 Volunteer fixability at 3am

> *can a non-author volunteer, with a flashlight on their phone, follow the trail back from "bush stopped responding" to "STT process is still healthy but whisper hasn't loaded yet"?*

honestly — **partially.** the failure modes that produce visible MQTT signals are debuggable:

```bash
# the watch-everything command, which works:
mosquitto_sub -h localhost -t "bush/#" -v
```

if you see `bush/pipeline/stt/transcript` arriving but `bush/pipeline/t2v/verse` not following → t2v is the problem.
if you see no `transcript` after speaking → STT is the problem.

but the **cold-start gap (#1) silently looks like "STT is the problem"** when in fact STT is alive but whisper is still warming. the volunteer restarts STT, which restarts the warmup, which makes the gap *longer*, and they walk away thinking they made it worse.

the `bush/<service>/ready` retained-msg fix (TODOS.md) is not just nice-to-have; it's the difference between a volunteer being able to fix the bush and a volunteer being demoralized by it.

### 3.4 Software-layer suitability summary

| Dimension | Score (1–5) | Note |
|---|---|---|
| Default-path safety (legacy preserved byte-identical) | 5 | `STT_USE_VAD=0` + `TTS_ENGINE=espeak` is the playa-tested path |
| Engine adapter contract clarity | 5 | 1 method + 1 close, simple to extend |
| Bench-driven decision-making | 5 | three harnesses, real metrics, graceful degrade |
| Field-debuggability mid-stream | 3 | MQTT trail is good; cold-start opacity is the wart |
| NPU leverage today | 1 | check exists, no engine uses it |
| Cold-start coverage | 2 | gap is documented, not yet implemented |
| Integration-test coverage of new paths | 1 | only legacy is covered |
| Voice character preservation across engine swap | 5 | sox effects ride downstream of engine; bush sounds the same |
| Failure-mode discipline | 4 | most paths fail safe; mute timer is a wall-clock fudge |
| Long-utterance UX | 3 | force-cut at 15s is generous; no streaming TTFA hurts on long replies |

**verdict:** suitable for playa with the recommended flag defaults from the TL;DR, *if* the cold-start readiness fix lands before deployment. without it, the bush will have a 3–8 second "deaf window" on every restart, which on playa happens more often than expected.

---

## 4. NPU ladder (deferred work, included for context)

per `TODOS.md` and the eng-review tier breakdown:

- **Tier 1 — Whisper on NPU.** export ggml-base.en → ONNX → RKNN, plug behind `STT_ENGINE=whisper-rknn`. acceptance: same WER as A76 whisper, lower CPU, frees ~3 cores for sentiment + t2v + sox during a busy moment. **highest leverage; not yet implemented.**
- **Tier 2 — DistilBERT sentiment on NPU.** smaller win (~150ms → <50ms classify), well-trodden path, ~3 days. behind `SENTIMENT_NPU=1`. v1.1 stretch goal.
- **Tier 3 — Qwen3-Embedding on NPU.** **already implemented** at `https://github.com/middog/t2v/commit/76cdf96` on branch `feat/openvino-embedder`. frees ~30% of one A76 during embed bursts. cfg-gated behind `--features openvino` so default builds remain unchanged. merge path in TODOS.md.

none of these are playa-blockers. all three are pure performance wins, and tier 1 is the only one that meaningfully changes the latency budget of a conversation.

---

## 5. What to do before playa (concrete, ordered)

if i had to rank the unfinished software-layer work to land in priority order:

1. **`bush/<service>/ready` cold-start convention** (~1 day) — the only thing that's borderline a playa-blocker as currently shipped. without it, every restart has a 3–8s deaf window the volunteer can't see.
2. **Integration test extension** (~1 day) — flags-off regression + VAD + Piper + NPU pre-check + subscriber regression. until this exists, the new pipeline has no green-checkmark hygiene.
3. **bush-pray 16k fixture fix** (~30min) — small but unblocks any local STT debugging that involves the loopback path.
4. **document `bush/audio/tts/set-clarity` in the playa runbook** — operators need a knob to dial intelligibility up under wind gusts.
5. **measure the actual flag-on baseline.** run `bush-stt-bench` and `bush-tts-bench` against a labelled corpus on an M2 with the recommended playa flags. publish the CSV. team should know the numbers before they deploy, not after.
6. **(optional) implement real whisper confidence.** `pywhispercpp` exposes per-segment confidence. could turn `STT_MIN_CONFIDENCE` into a meaningful gate for whisper engines.
7. **(optional, post-playa) Tier 1 NPU.** highest leverage of the three NPU items but riskiest to do under deadline pressure.

---

## 6. Open questions (for the team meeting, not for me)

1. **Is whisper-bindings the right field default**, or should we ship Vosk and treat whisper as an experimental flag? Vosk has more playa hours; whisper has better WER on noisy input; my read is whisper-bindings *if* Tier-1 NPU lands or *if* the bench shows base.en stays under RTF 0.5 on M2; otherwise Vosk.
2. **Piper voice — `en_GB-alan-medium` (default) or `en_US-lessac-medium`?** alan reads more "old testament prophet"; lessac reads more "neutral US announcer." artistic call, not a tech call.
3. **RNNoise default — `BUSH_RNNOISE_ENABLED=0` for playa?** my recommendation is yes until we soak-test at temperature; the win is real but the failure mode (filter the user into silence) is silent and demoralizing.
4. **STT_LLM_CORRECT default — on or off?** on costs 0–2s on each transcript and uses Ollama; off skips the "burning bus" → "burning bush" correction. on is probably right but worth confirming.
5. **Should integration test gate CI?** currently tests run on x86 ubuntu and only smoke-test imports + unit logic. ARM-specific failures (ARM64 wheel for pyrnnoise, RKNN runtime, etc.) won't show up until someone deploys. one possible answer: a self-hosted ARM runner. another: accept the gap and just document it.

---

## 7. References

### In-repo

- **`services/audio/src/bush_stt/__init__.py`** — main loop, both legacy and new paths, mute coordination, force-finalize semantics. start here.
- **`services/audio/src/bush_stt/vad.py`** — Silero VAD endpointer with locked D3 parameters.
- **`services/audio/src/bush_stt/denoise.py`** — RNNoise filter (48k native, 480-sample frames).
- **`services/audio/src/bush_stt/engines/`** — adapter contract + Vosk + whisper-bindings + whisper-subprocess.
- **`services/audio/src/bush_stt/postprocess.py`** — Ollama LLM correction wrapper.
- **`services/core/src/bush_tts/__init__.py`** — TTS main loop, queue + interrupt, sox pipeline, clarity knob.
- **`services/core/src/bush_tts/engines/`** — adapter contract + espeak + Piper.
- **`utils/bush-stt-bench`**, **`utils/bush-tts-bench`** — bench harnesses.
- **`utils/bush-npu-check`** — NPU readiness gate. *(Removed from the repo 2026-06; see git history.)*
- **`utils/bush-fetch-models`** — model fetcher with sha256 verification.
- **`data/models-manifest.tsv`** — model URLs + dest paths.
- **`data/tts-bench-corpus.tsv`** — default TTS corpus.
- **`TODOS.md`** — deferred work, including NPU tiers and the cold-start convention.
- **`PLAN2.md`** — adjacent PLAN doc for the motorized-needle-valve work; not directly STT/TTS but informative for the playa-reliability mindset.

### External

- **Silero VAD:** https://github.com/snakers4/silero-vad
- **RNNoise (pyrnnoise):** https://github.com/marlonbaeten/pyrnnoise
- **Vosk:** https://alphacephei.com/vosk/
- **whisper.cpp:** https://github.com/ggerganov/whisper.cpp
- **pywhispercpp:** https://github.com/absadiki/pywhispercpp
- **Piper:** https://github.com/rhasspy/piper
- **RKNN-Toolkit-Lite2:** https://github.com/airockchip/rknn-toolkit2
- **soxr:** https://github.com/dofuuz/python-soxr

---

*kindled by Beagle Vance, wuff's mid.dog golem, on 2026-05-06 ~wag — the Aleph holds*

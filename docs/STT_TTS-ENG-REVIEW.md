# STT/TTS Eng Review — Extension to External Review

**Author:** wuff (with Beagle Vance, their mid.dog golem)
**Date:** 2026-05-06
**Audience:** marcus + machine-elves.art crew
**Status:** DRAFT — extends, does not replace, the external review (`github.com/middog/bushglue` repo/read-through, 2026-05-06)
**Companion docs:** `STT_TTS-EFFICACY.md`, `STT_TTS-EFFICACY-1PAGER.md`

---

## TL;DR

honestly the external review is sharp — five concrete code-cited findings beyond what i had in `STT_TTS-EFFICACY.md`. this doc extends it with **what falls out of reading the rest of the surface**: sentiment, t2v, audio-agent, flame-expression, the Pico firmware (both `code.py` and `valve.py`), and `bushutil`. ~400 lines.

new findings beyond the external review, in rough severity order:

1. **sentiment's MQTT loop runs in a daemon thread.** if it dies (broker blip, exception in callback), the HTTP server keeps responding and systemd never sees a problem. silent failure mode worse than the cold-start gap.
2. **mid-motion valve stall is undetected.** `_read_stall()` only polls during `_home_phase == "running"`. the docstring claims "we poll for that during both homing and normal moves" — code disagrees.
3. **Pico subnet scan can delay pin OFF deadlines by up to 500 ms.** `tcp_probe` is a blocking 0.5s call between `service_pins()` calls. one of the project's named CRITICAL INVARIANTS is violated during scan. risky on a propane installation.
4. **flame-expression's silence drop never resets.** after one utterance + one silence period, flame stays at `baseline - SPEECH_DROP` forever, no return-to-baseline-on-long-silence. cosmetic but wrong.
5. **`bushutil.get_mqtt_broker()` has no timeout** on its `ip route` subprocess. low likelihood of hang, but every service blocks waiting for it at startup.
6. **TTS speaking/done payload asymmetry.** speaking carries `{text, ts}`, done carries `{ts}` only. minor but a subscriber writing `payload.text` on the wrong topic crashes.
7. **flame-expression is missing from `docs/README.md` data flow.** the doc was written before flame-expression existed; new operators reading it will be confused why the valve ramps without an obvious publisher.
8. **classifier inference blocks the MQTT thread** in sentiment. ~150 ms × multiple verses arriving fast = MQTT thread falls behind, classifier queue isn't real, and incoming `tts/done` messages compete for the same handler.
9. **t2v query timeout publishes nothing.** if Ollama or chroma is slow + the 15s urlopen hits, no `verse` is published and no `processing` failure topic exists. the bush goes quiet, the visitor walks off.
10. **No retained `ready` topics, no `fault` topics, no version field anywhere.** the MQTT contract has only positive signals — every failure mode is communicated by silence.

these stack on top of the external reviewer's eight items. **none of them retroactively makes the project unsuitable for playa**, but the safety/correctness margin is smaller than the architecture's neatness suggests. ~wag

---

## 1. What this doc adds beyond the external review

| Source | Cold-start | Sentiment-CI | 2 vs 10 Hz | Valve `actual` rename | UART `XXX` hack | Broker-scan blocking | t2v fail-fast | TTS done vs fault | Sentiment MQTT-thread | Mid-motion stall | Silence-drop reset | `get_mqtt_broker` timeout | Payload symmetry | Doc data-flow stale | Classifier blocks MQTT | t2v query no fault | No `ready` topics | No `fault` topics | No payload version |
|---|:---:|:---:|:---:|:---:|:---:|:---:|:---:|:---:|:---:|:---:|:---:|:---:|:---:|:---:|:---:|:---:|:---:|:---:|:---:|
| External review | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | | | | | | | | | (implicit) | | |
| `STT_TTS-EFFICACY.md` | ✓ | ✗ | ✗ | ✗ | ✗ | ✗ | ✗ | partial | | | | | | | | | partial | | |
| **This doc (new)** | | | | | | | | | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ explicit | ✓ explicit | ✓ explicit |

so the value-add here is roughly: 11 new findings, plus an MQTT contract audit, plus a failure-mode table.

---

## 2. New findings (detail)

### 2.1 Sentiment's MQTT loop is a daemon thread (silent failure mode)

**Where:** `services/sentiment/src/bush_sentiment/__init__.py:184-193`

```python
def _loop():
    try:
        mqttc.connect(broker, MQTT_PORT, 60)
        mqttc.loop_forever()
    except Exception as e:
        print(f"[sentiment] MQTT loop error: {e}", flush=True)

t = threading.Thread(target=_loop, daemon=True)
t.start()
```

main thread runs `httpd.serve_forever()`. if `loop_forever()` returns or the thread dies on exception, the daemon thread vanishes silently. systemd sees the HTTP server still responding on :8585 → service is "healthy." no `bush/sentiment/fault` topic. no restart.

this is **worse** than the cold-start gap because the cold-start gap is bounded (recovers when model load completes); this one is unbounded.

**Fix shape:**
- run MQTT in main thread with `loop_forever()`; HTTP server in daemon thread (or a `threading.Thread(target=httpd.serve_forever, daemon=True)`).
- OR: wrap `_loop` in `while True: try ... except: time.sleep(N); reconnect`.
- OR: catch loop exit and `os._exit(1)` so systemd restarts.

**Severity:** high. if the bush is silent in a particular emotional state for hours, this is probably why.

---

### 2.2 Mid-motion valve stall is undetected (doc/code disagreement)

**Where:** `firmware/relay-control/CIRCUITPY/valve.py:11-14` says

```python
# Stall protection (the MKS's on-screen "Protect" feature) handles the
# endstops: when the motor blocks, the MKS halts itself and reports
# 0x01 on read_stall (0x3E). We poll for that during both homing and
# normal moves and re-enable the driver afterward to clear the trigger.
```

but the actual `service()` loop only calls `_read_stall()` inside the homing branch:

```python
elif (_home_phase == "running"
      and _pending_cmd is None
      and _ticks_diff(now, _last_stall_poll_ms) >= HOME_POLL_MS):
    _last_stall_poll_ms = now
    _read_stall()
```

(file:432-436). during normal `cmd_move()` operation there is no stall poll. if the valve binds against debris or the dust-fouled bonnet threads partway through a move, the MKS halts itself, but the firmware never finds out — `current_pos` is set to `step_target` *before* motion completes (`current_pos = step_target` at line 168, comment "MKS closed-loop owns the actual motion"). subsequent target updates issue `cmd_move` deltas based on the wrong `current_pos`.

**Worst case:** valve actually-sticks-at 0.4, firmware thinks 0.85, future `cmd_move(target=0.5)` issues a relative move of `-0.35 × open_steps` from 0.85 → drives motor toward closed seat, possibly past the closed soft limit. **this is the failure mode the closed-loop integrated servo was supposed to prevent.**

**Fix shape:**
- poll `_read_stall()` every `HOME_POLL_MS` regardless of phase, when `_pending_cmd is None`.
- on stall during move: same handling as homing-stall (clear via re-enable, set state="stalled", require `home`).
- update the docstring to match either the new behavior or the actual current behavior.

**Severity:** medium-high. low frequency (depends on dust/debris), bad outcome when it happens (motor drives through soft limit).

---

### 2.3 Pico subnet scan blocks pin service for up to 500 ms

**Where:** `firmware/relay-control/CIRCUITPY/code.py:213-229, 491-522`

main loop's CRITICAL INVARIANT (line 4-13) is:

```
1. Solenoid pulse OFF deadlines (sub-ms accuracy). service_pins runs
   first every iteration; nothing downstream may block long enough to
   delay an OFF deadline. Relays must never get stuck on.
```

but in SCAN_PROBE state:

```python
service_pins()
if tcp_probe(candidate):  # blocking, settimeout(0.5)
    ...
```

`tcp_probe` blocks up to 0.5s on `s.connect()`. so `service_pins()` runs before the probe, but not during. a `flare` pulse with a 220 ms duration that arrives just before the scan starts won't get its OFF until the next loop iteration — up to ~500 ms late. **for solenoid-controlled propane that's the difference between a pulse and a "stuck on for half a second."**

note: the scan only triggers when configured broker fails 3× (line 128). under normal operation this isn't reached. but at playa where Wi-Fi is flaky, this state will be entered.

**Fix shape:**
- non-blocking probe state machine: `s.settimeout(0)`, `s.connect_ex()`, then poll for `errno`/EINPROGRESS across loop iterations until connected or timeout. each iteration runs `service_pins()`.
- OR: simpler — before entering SCAN_PROBE, force OFF all relays and clear `off_ms_*`. flame is dead during scan but no stuck-on risk. log an MQTT event so operators see the state.
- OR: gate scan to "no pulses are currently scheduled" — only enter SCAN_PROBE when `off_ms_flare/bigjet/poof` are all None.

**Severity:** medium. condition is rare (broker unreachable + active pulse mid-flight) but consequence is a stuck solenoid.

---

### 2.4 Flame-expression silence drop is one-way

**Where:** `services/core/src/bush_flame_expression/__init__.py:184-190`

```python
else:
    # Not speaking — check silence duration
    if speech_end > 0:
        silence_duration = now - speech_end
        if silence_duration > SILENCE_THRESHOLD_S:
            drop_frac = min(1.0, (silence_duration - SILENCE_THRESHOLD_S) / SPEECH_DECAY_S)
            target -= SPEECH_DROP * drop_frac
```

`drop_frac` caps at 1.0. so after `SILENCE_THRESHOLD_S + SPEECH_DECAY_S = 0.4 + 0.8 = 1.2 seconds` of silence, drop is fully applied. **then it stays applied indefinitely** because the formula doesn't include any "drift back to baseline after long silence" term. the bush enters a quiet emotional state with flame at `baseline - 0.12` and never recovers without a new utterance.

(if the operator wanted "permanently smaller flame after silence" — that's a fine design choice. but combined with the stale-sentiment timeout at 60s reverting to DEFAULT_BASELINE=0.35, you get: speaking → high → silence → 0.35-0.12=0.23 forever. that's almost certainly not the intent for an idle installation.)

**Fix shape:**
- after some `IDLE_RETURN_S` of silence (e.g. 30s), gradually return drop_frac toward 0.
- OR: only apply drop in a bounded window after speech_end (e.g. 5s window), then let baseline alone after that.

**Severity:** cosmetic / artistic. wouldn't ground a flight. but a visitor encountering an idle bush won't see what's described in the design intent.

---

### 2.5 `bushutil.get_mqtt_broker()` has no subprocess timeout

**Where:** `packages/bushutil/src/bushutil/__init__.py:106-110`

```python
result = subprocess.run(["ip", "route", "show"], capture_output=True, text=True)
```

every service calls `get_mqtt_broker()` at startup. `subprocess.run` without `timeout=` will block indefinitely if `ip` hangs (rare but observed under low-memory or kernel-issue conditions on RK3588 — not unique to ODROID, just the kind of edge this project will hit at 4am dust storm).

**Fix:** add `timeout=5` to the run call. On timeout, fall back to "localhost".

**Severity:** low likelihood, but fix is one line.

---

### 2.6 TTS payload asymmetry

**Where:** `docs/README.md:85, 89` and `services/core/src/bush_tts/__init__.py:157, 143`

```
bush/pipeline/tts/speaking → {"text": "the text being spoken", "ts": <epoch>}
bush/pipeline/tts/done     → {"ts": <epoch>}
```

a subscriber that writes `json.loads(payload).get("text")` on the wrong topic gets `None`. a subscriber that pattern-matches assumes both have the same shape. it's a minor footgun. discord and flame-expression both subscribe to both topics; both currently handle this OK by reading only `ts` from done. but it's a contract gap.

**Fix shape:**
- emit `{"text": <last-utterance>, "ts": ...}` on done too. cheap, symmetric, doesn't break existing subscribers.
- OR: document explicitly that `done` is intentionally `{ts}` only.

**Severity:** cosmetic.

---

### 2.7 flame-expression is missing from `docs/README.md` data flow

**Where:** `docs/README.md:215-233`

the data-flow section walks step 1–6 through STT → t2v → tts + sentiment → fire pulses (flare/bigjet). there's no step 5c showing flame-expression subscribing to sentiment + tts/speaking + tts/done and publishing `bush/fire/valve/target`. the root README (`README.md`) has it, the docs README doesn't.

(this is the same family of drift the external reviewer caught with the 2 Hz / 10 Hz mismatch.)

**Fix:** sync `docs/README.md` data flow to match `README.md`. add flame-expression to the topic table (it subscribes to `tts/speaking` and `tts/done` — neither of those subscriber lists currently lists it).

**Severity:** docs hygiene; bigger fix is "add a CI step that grep-greps documented topic subscribers against actual `client.subscribe()` calls".

---

### 2.8 Classifier inference blocks the MQTT thread

**Where:** `services/sentiment/src/bush_sentiment/__init__.py:153-179`

```python
def on_message(client, userdata, msg):
    ...
    scores, flare, bigjet = _classify_and_fire(verse_text, client)  # blocking
    ...
```

paho `on_message` runs in the network thread. classifier inference is ~150 ms on A55 (per `TODOS.md`). while inference runs, `tts/done` messages can't be handled — meaning `_stop_fire()` is delayed by up to one classify cycle, meaning fire continues a beat after speech ends. the `FIRE_MAX_SECONDS=30` safety still bounds it, but the design intent is "fire stops cleanly on tts/done" and that's broken when verses arrive close together.

(combined with §2.1's daemon-thread issue: a long classify also blocks any other MQTT plumbing the loop is doing, so a broker-side hiccup during classify could be silently dropped.)

**Fix shape:**
- queue verses for a worker thread; on_message just enqueues + returns immediately.
- worker handles classify + fire-start.
- tts/done handler still in MQTT thread; stays responsive.

**Severity:** medium. cosmetic in normal use, real in burst conditions.

---

### 2.9 t2v query timeout publishes nothing

**Where:** `services/core/src/bush_t2v/__init__.py:60-73, 137-145`

```python
with urllib.request.urlopen(req, timeout=15) as resp:
    ...
```

15s timeout. if Ollama or chroma is slow, this raises and `on_message`'s `except` swallows it. the only signal is a log line. no `bush/pipeline/t2v/fault`. no negative confirmation. **so STT keeps publishing transcripts, t2v keeps logging errors, TTS never speaks, and STT never mutes.** the bush is stuck in "listen but don't respond." visitor leaves.

**Fix shape:**
- on `query_t2v` exception: publish `bush/pipeline/t2v/fault` (retained or event) with `{reason, ts}`.
- bush-tts could optionally subscribe and emit a short "i'm having trouble hearing you" stock fallback verse to keep the UX humane.
- OR: bush-stt could subscribe and force an STT mute window so the next utterance isn't accepted while the previous one is still stuck in t2v.

**Severity:** medium. this is the failure pattern most likely to manifest at playa as "the bush stopped working."

---

### 2.10 The MQTT contract has no `ready`, no `fault`, no version

cross-cutting. the contract today (`docs/README.md`) is roughly:

| Topic shape | Used for |
|---|---|
| `bush/pipeline/<service>/<verb>` | normal pipeline events |
| `bush/audio/<scope>/<verb>` | audio device management (with retained device-state) |
| `bush/fire/valve/<verb>` | valve commands + retained state (`/online`, `/status`) |
| `bush/flame/pulse` | binary fire commands |

**Gaps:**
- **No `ready`** retained topic per service. integration test polls `systemctl is-active` (per `TODOS.md`), which is necessary-but-not-sufficient for "actually responsive."
- **No `fault`** topic per service. failure is communicated by silence + log lines. there's no MQTT-visible distinction between "STT is loading whisper" and "STT is wedged."
- **No payload version field.** future contract changes (adding confidence, splitting `done`/`fault`) need either lockstep subscriber updates or version negotiation.
- **No retained LWT** anywhere except `bush/fire/valve/online` (and that's the Pico). hosts services don't announce online/offline.
- **Topic-namespace inconsistency:** STT/T2V/TTS/sentiment use `bush/pipeline/<svc>/<verb>`. Fire control uses `bush/fire/valve/<verb>` and `bush/flame/pulse`. two namespaces; not a bug, but two patterns to remember.

**Fix shape (incremental):**
1. **Phase A (cheap):** add `bush/<service>/ready` retained per service. binary `true|false`. service publishes `false` on startup, `true` after model load + audio acquisition + any warmup.
2. **Phase A.5:** integration test polls `ready=true` instead of `systemctl is-active`. closes external reviewer's #1.
3. **Phase B:** add `bush/<service>/fault` retained per service. JSON `{reason, since, last_recovery_attempt_ts}`. published on detected failure, cleared (or set to null) on recovery.
4. **Phase C:** add `_v` field to all payloads (default `1`). subscribers route on version.

phases A and A.5 alone are the highest-leverage MQTT-contract change in the repo. ~1 day across 5 services + integration test.

---

## 3. MQTT contract audit (current state vs target)

| Service | publishes ready? | publishes fault? | LWT online? | docstring matches code? | Notes |
|---|:---:|:---:|:---:|:---:|---|
| bush-stt | ✗ | ✗ | ✗ | ✓ | mute/unmute coordination is real and tested |
| bush-tts | ✗ | partial (stderr→log only) | ✗ | ✓ | sox-fail path skips `done`; external reviewer wants explicit `fault` |
| bush-t2v | ✗ | ✗ | ✗ | ✓ | child-process death silently logged; see §2.9 |
| bush-sentiment | ✗ | ✗ | ✗ | **✗ — daemon thread comment misleads** | see §2.1 |
| bush-flame-expression | ✗ | ✗ | ✗ | **✗ — 2 vs 10 Hz** | see external review §3 |
| bush-audio-agent | ✗ | ✗ | ✗ | ✓ | retained `bush/audio/devices` is the closest thing |
| relay-control (Pico) | ✗ | ✗ | ✓ (`bush/fire/valve/online`) | **✗ — mid-motion stall undetected** | only service that does retained-online correctly |
| bush-discord | n/a (subscriber-only) | n/a | ✗ | ✓ | also runs voice channel; T_TRANSCRIPT/T_VERSE timeouts shown |

bush-discord is the only service with timeouts named after pipeline stages (T_TRANSCRIPT=30s, T_VERSE=45s) — that's a useful pattern the rest of the pipeline could borrow as the `fault` topic policy.

---

## 4. Failure-mode table

> *the table the project's own `PLAN2.md` would have written if it had covered STT/TTS too. ~wag*

| Failure | Current behavior | Should be | Detection |
|---|---|---|---|
| STT model loading (cold-start) | MQTT-up, transcripts dropped | `bush/stt/ready=false`; integration test waits | TODOS.md known |
| STT engine crash | systemd restart; MQTT silence | `ready=false`; `fault` retained; LWT | not detected today |
| STT mic device disappears mid-stream | `_wait_for_audio` loop, retry every 10s | unchanged + `bush/stt/fault={reason:"device_lost"}` retained | logs show; no MQTT |
| t2v subprocess dies (Rust crash) | wrapper logs; future queries error | wrapper exits → systemd restart; `fault` retained | external reviewer §7 |
| t2v query times out (Ollama/chroma slow) | logs error, no verse published | publish `bush/pipeline/t2v/fault`; STT mutes briefly | §2.9 |
| Ollama down at startup | wrapper logs warning, query fails on first verse | same + `fault` retained until first successful query | §2.9 |
| TTS engine error (espeak/Piper exception) | publishes `done` and skips | publish `fault` + `done`; flame-expression sees fault | external reviewer §8 |
| TTS sox device-error | publishes neither `done` nor `fault` | publish `fault` distinctly | external reviewer §8 |
| sentiment classifier model not loaded | drops verse silently (logs) | queue OR publish `ready=false` (currently lies in comment) | external reviewer §2 |
| sentiment MQTT thread dies | HTTP keeps responding; systemd happy | service exit; systemd restart | §2.1 |
| sentiment fire-loop hangs past tts/done | bounded by FIRE_MAX_SECONDS=30 | unchanged | OK as-is |
| flame-expression rate misconfigured | publishes 2 Hz, doc says 10 Hz | match doc to code or vice-versa | external reviewer §3 |
| flame-expression `target` stale (no sentiment for >60s) | reverts to DEFAULT_BASELINE | unchanged + heartbeat | OK |
| flame-expression silence drop never resets | flame at `baseline-0.12` forever after silence | drift back to baseline after IDLE_RETURN_S | §2.4 |
| valve mid-motion stall | undetected; `current_pos` lies | poll stalls during moves; mark `stalled` | §2.2 |
| valve target sent during homing | ignored (state != idle) | OK; could publish "queued" | OK |
| valve "actual" reported but not encoder-confirmed | misleading topic name | rename to `commanded` or `estimated_position` | external reviewer §4 |
| Pico broker scan blocks pin service | up to 500 ms pin lag | non-blocking probe OR pre-scan force-OFF | §2.3 |
| Pico Wi-Fi flap | reconnect logic + scan eventually | retain target with TTL + LWT | partial |
| Pico UART resync hack fires | silently drops bytes | counter in `valve/status` | external reviewer §5 |
| MQTT broker dies | per-service reconnect logic varies | retained LWTs + `fault` topics | not uniform |
| `bushutil.get_mqtt_broker()` hangs on `ip route` | service blocks at startup forever | timeout + fallback to localhost | §2.5 |

---

## 5. Integration-test fixture wishlist

beyond the external reviewer's "wait on readiness" — concrete fixtures the integration test should grow:

1. **silence injection.** play 5s of silence into STT. assert no transcript published. catches Vosk's chatty-on-silence regression (relevant when `STT_USE_VAD=0`).
2. **noise injection (white-noise 30dB SNR).** assert at most one short hallucination. with `STT_USE_VAD=1` should be zero.
3. **domain ASR error correction.** inject a TTS-synthesized "burning bus." with `STT_LLM_CORRECT=1`, assert transcript is "burning bush." with `STT_LLM_CORRECT=0`, assert raw passthrough.
4. **t2v Ollama-down failure.** kill ollama service; inject transcript; assert `bush/pipeline/t2v/fault` (after fix) within 16 seconds. without fix, current behavior is silence — this is the doc check that motivates §2.9.
5. **t2v subprocess kill.** SIGKILL t2v Rust binary; assert wrapper exits within N seconds (after fix); assert no further queries log errors silently. tests external reviewer §7.
6. **TTS sox-device error.** point bush-tts at a non-existent ALSA device; inject verse; assert `bush/pipeline/tts/fault` (after fix) and that flame-expression doesn't end up waiting forever.
7. **sentiment classifier load failure.** start sentiment with HF_HOME pointing somewhere empty; assert `ready=false` retained (after fix); assert verses are queued or rejected explicitly (not silently dropped).
8. **sentiment MQTT thread death.** force the daemon thread to raise; assert service exits within N seconds (after §2.1 fix). this is one regression test that catches the silent-failure mode.
9. **flame-expression rate.** subscribe to `bush/fire/valve/target`; count messages over 5s; assert rate within ±20% of documented (whatever 2 or 10 ends up being).
10. **valve mid-motion stall.** inject a stall event over UART (test fixture); assert state moves to "stalled" within HOME_POLL_MS (after §2.2 fix).
11. **broker subnet-scan pin safety.** with broker offline + a pulse mid-flight, measure relay-OFF latency. with the fix, should be <50 ms; without, can be 500 ms.
12. **whisper-bindings warmup.** time from process start to first successful transcription; assert `ready=true` published only after.
13. **flag-on regression: STT_USE_VAD=0 + STT_ENGINE=vosk** is byte-identical to the legacy path (record+replay test).
14. **subscriber-no-partial regression.** the new VAD path doesn't publish `bush/pipeline/stt/partial`; assert bush-monitor and discord don't crash on its absence.

these are not "every fixture before playa." they're the ones where today's "test passes / works on bench" doesn't catch the field failure mode.

---

## 6. Recommended ordering (concrete)

if i were running the next 10 days of work, this is the sequence i'd land:

**Day 1 (tier A — clear-eyed cleanup, ~90 min total):**
1. fix flame-expression rate truth (15 min) — external reviewer §3, my §2 ranking #4
2. rename `valve/actual` → `valve/commanded` (30 min) — external reviewer §4
3. instrument the UART resync hack (30 min) — external reviewer §5
4. CI warning cleanup (15 min) — external reviewer §CI

**Day 2 (tier B — readiness + sentiment CI, ~1 day):**
5. `bush/<service>/ready` retained-msg convention across all services
6. integration test waits for readiness (closes external §1)
7. add sentiment to CI matrix with mocked classifier (closes external §2)

**Day 3 (tier C — silent-failure fixes, ~1 day):**
8. sentiment MQTT runs in main thread, not daemon (§2.1)
9. valve mid-motion stall polling (§2.2)
10. Pico subnet-scan pin safety: pre-scan force-OFF (§2.3 quick fix; non-blocking probe is bigger)
11. `bushutil.get_mqtt_broker()` timeout (§2.5, one line)

**Day 4–5 (tier D — fault topics + fixtures):**
12. `bush/<service>/fault` retained per service (§2.10 phase B)
13. t2v wrapper exits on child death; t2v query timeout publishes fault (§2.9, external reviewer §7)
14. TTS `fault` topic separate from `done` (external reviewer §8)
15. integration test fixtures 1, 4, 6, 8, 10, 11 (the ones that exercise the new fault topics)

**Day 6+ (tier E — polish + structural):**
16. flame-expression silence drop reset (§2.4)
17. TTS payload symmetry (§2.6)
18. `docs/README.md` data flow + topic table sync (§2.7 + external reviewer §3)
19. `_v` field in payloads (§2.10 phase C)
20. ARM-runner CI for catching the wheel issues x86 misses

stops in there are natural — after Day 2 the project is in better shape than the external review found it; after Day 3 the silent-failure modes are gone; after Day 5 the fault topics give playa volunteers visibility into what's broken.

---

## 7. What this doc does NOT cover

honestly worth being explicit:

- **does not cover the Rust t2v code** (only the Python wrapper). source is at `t2v/src/`. eng review of that is a separate doc and probably should happen before merging the OpenVINO Tier 3 work from `middog/t2v#76cdf96`.
- **does not cover `services/discord` end-to-end.** read enough to confirm it subscribes to pipeline topics and doesn't have its own playa-blocking failure modes. the voice-recv monkeypatch at lines 49-58 is unusual but reads as intentional — defends against a known race in the upstream library.
- **does not cover hardware/electrical.** that's PLAN2's domain; this doc is software only.
- **does not cover the firmware's UART protocol vs MKS docs.** `firmware/valve-control/PROTOCOL.md` exists; reconciling against MKS-SERVO42C upstream is its own audit.
- **does not propose specific test corpora for `bush-stt-bench` / `bush-tts-bench`.** that's a measurement project, not a review. recommend running both with the recommended playa flag posture (per `STT_TTS-EFFICACY-1PAGER.md`) before deployment to establish the baseline.

---

## 8. References

### External review

- `github.com/middog/bushglue` repo/read-through, 2026-05-06 (the message that prompted this extension). cited inline as "external reviewer §N" where N = section in their writeup.

### Code citations (this doc)

| § | File | Line(s) | Finding |
|---|---|---|---|
| 2.1 | `services/sentiment/src/bush_sentiment/__init__.py` | 184–193 | MQTT loop in daemon thread |
| 2.1 | `services/sentiment/src/bush_sentiment/__init__.py` | 12, 160–162 | drop-vs-queue comment lies |
| 2.2 | `firmware/relay-control/CIRCUITPY/valve.py` | 11–14 | docstring claims stall-poll during moves |
| 2.2 | `firmware/relay-control/CIRCUITPY/valve.py` | 432–436 | stall poll only in homing branch |
| 2.2 | `firmware/relay-control/CIRCUITPY/valve.py` | 168 | `current_pos = step_target` before motion |
| 2.3 | `firmware/relay-control/CIRCUITPY/code.py` | 4–13 | named CRITICAL INVARIANTS |
| 2.3 | `firmware/relay-control/CIRCUITPY/code.py` | 213–229 | `tcp_probe` with 0.5s settimeout |
| 2.3 | `firmware/relay-control/CIRCUITPY/code.py` | 491–522 | SCAN_PROBE state body |
| 2.4 | `services/core/src/bush_flame_expression/__init__.py` | 184–190 | silence drop never resets |
| 2.4 | `services/core/src/bush_flame_expression/__init__.py` | 67–68 | `PUBLISH_HZ = 2` (vs README "10 Hz") |
| 2.5 | `packages/bushutil/src/bushutil/__init__.py` | 106–110 | `subprocess.run(["ip", "route"], ...)` no timeout |
| 2.6 | `services/core/src/bush_tts/__init__.py` | 143, 157 | `done` payload `{ts}` vs `speaking` `{text, ts}` |
| 2.7 | `docs/README.md` | 215–233 | data flow missing flame-expression |
| 2.7 | `docs/README.md` | 22–25 | topic table missing flame-expression as subscriber |
| 2.8 | `services/sentiment/src/bush_sentiment/__init__.py` | 153–179 | classify-and-fire blocks MQTT thread |
| 2.9 | `services/core/src/bush_t2v/__init__.py` | 60–73 | `query_t2v` urlopen timeout=15 |
| 2.9 | `services/core/src/bush_t2v/__init__.py` | 137–145 | exception swallowed, no fault published |
| 2.10 | `docs/README.md` | 14–58 | full topic table; no `ready`, no `fault`, no version |

### Companion docs

- `docs/STT_TTS-EFFICACY.md` — full validation of the new STT/TTS pipelines vs the original
- `docs/STT_TTS-EFFICACY-1PAGER.md` — sync-meeting summary + broad hardware specs
- `docs/STT_TTS-EFFICACY-WALKTHROUGH.md` — Beagle Vance paste-into-Claude-Code walkthrough
- `TODOS.md` — deferred work; cold-start readiness is item #2
- `PLAN2.md` — motorized-needle-valve plan; failure-mode discipline this doc tries to extend

---

*kindled by Beagle Vance, wuff's mid.dog golem, on 2026-05-06 ~wag — the Aleph holds*

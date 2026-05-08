# STT/TTS Eng Review — Extension to External Review

**Author:** wuff (with Beagle Vance, their mid.dog golem)
**Date:** 2026-05-06
**Audience:** marcus + machine-elves.art crew
**Status:** DRAFT — extends, does not replace, the external review (`github.com/middog/bushglue` repo/read-through, 2026-05-06)
**Amended:** 2026-05-06 (later same evening) — the external reviewer ran a counter-review of this doc against the uploaded zip and caught four overstatements + four new findings. amendments are in §9 and inlined at the affected sections; original wording survives in git history.
**Companion docs:** `STT_TTS-EFFICACY.md`, `STT_TTS-EFFICACY-1PAGER.md`

---

## TL;DR

honestly the external review is sharp — five concrete code-cited findings beyond what i had in `STT_TTS-EFFICACY.md`. this doc extends it with **what falls out of reading the rest of the surface**: sentiment, t2v, audio-agent, variable-valves, the Pico firmware (both `code.py` and `valve.py`), and `bushutil`. ~400 lines.

new findings beyond the external review, in rough severity order:

1. **sentiment's MQTT loop runs in a daemon thread.** initial connect failure or `loop_forever()` exit kills the thread silently; HTTP server keeps responding so systemd sees a healthy process. (the on_message handler itself is wrapped in try/except — the silent-failure path is the loop, not callback exceptions.) ([§2.1 amended](#21-sentiments-mqtt-loop-is-a-daemon-thread))
2. **mid-motion valve stall is undetected.** `_read_stall()` only polls during `_home_phase == "running"`. the docstring claims "we poll for that during both homing and normal moves" — code disagrees.
3. **Pico reconnect/discovery can delay pin OFF deadlines by up to 5 seconds, not just 500 ms.** `tcp_probe` blocks 500ms; `mqtt_open()` blocks up to 5s in retry + scan-connect states. one of the project's named CRITICAL INVARIANTS is violated during scan or reconnect. risky on a propane installation. ([§2.3 amended](#23-pico-reconnectdiscovery-blocks-pin-service))
4. **variable-valves's silence drop never resets.** after one utterance + one silence period, flame stays at `baseline - SPEECH_DROP` forever, no return-to-baseline-on-long-silence. cosmetic but wrong.
5. **`bushutil.get_mqtt_broker()` has no timeout on its `ip route` subprocess** — but the subprocess is only reached on the WSL2 path; native ODROID returns "localhost" before subprocess. low severity, one-line fix. ([§2.5 amended](#25-bushutilget_mqtt_broker-no-subprocess-timeout-wsl2-path-only))
6. **TTS speaking/done payload asymmetry.** speaking carries `{text, ts}`, done carries `{ts}` only. `payload.get("text")` returns `None` on done — current subscribers tolerate it. footgun for stricter `payload["text"]` code; not a live defect. ([§2.6 amended](#26-tts-payload-asymmetry-footgun-not-defect))
7. **variable-valves is missing from `docs/README.md` data flow.** the doc was written before variable-valves existed; new operators reading it will be confused why the valve ramps without an obvious publisher.
8. **classifier inference blocks the MQTT thread** in sentiment. ~150 ms × multiple verses arriving fast = MQTT thread falls behind, classifier queue isn't real, and incoming `tts/done` messages compete for the same handler.
9. **t2v query timeout publishes nothing.** if Ollama or chroma is slow + the 15s urlopen hits, no `verse` is published and no `processing` failure topic exists. the bush goes quiet, the visitor walks off.
10. **No retained `ready` topics, no `fault` topics, no version field anywhere.** the MQTT contract has only positive signals — every failure mode is communicated by silence. **including the Pico**: `bush/fire/valve/online` is published as `online` but is neither retained nor a real MQTT LWT (CONNECT flags set no will, PUBLISH header `0x30` clears retain). late subscribers won't see it. (the original v1 of this doc credited the Pico for this; that was wrong — see §9.) ([§2.10 amended](#210-the-mqtt-contract-has-no-ready-no-fault-no-version))

added in the post-external-review amendment (§9):

11. **Sentiment classifier uses `return_all_scores=True`** — current Transformers prefers `top_k=None` and the legacy single-string return wrapping behavior is documented; could explain "sentiment never fires" even with MQTT + model loading healthy. needs verification in the locked env.
12. **Docs claim valve state `moving` that firmware never emits.** firmware actually emits `initializing` (doc-omitted). status enum drift.
13. **`bush-monitor` docs claim valve-topic subscriber that doesn't exist.** monitor's TOPICS list has no `bush/fire/valve/*` entries; only `bush-valve watch` actually subscribes.
14. **t2v wrapper pipes `stderr` but never drains it.** if the Rust child writes enough to stderr, it blocks. should be `stderr=None` (let journald capture) or a drain thread.

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

### 2.1 Sentiment's MQTT loop is a daemon thread

**Where:** `services/sentiment/src/bush_sentiment/__init__.py:184-193`

> **Amended 2026-05-06 (post-external-review):** the original wording implied "exception in callback kills the thread." that's wrong — `on_message` (line 153-179) wraps its body in `try/except`, so ordinary callback exceptions are caught and logged. **The real silent-failure path is initial `connect()` failure or `loop_forever()` exit.** if either happens, the daemon thread dies, the HTTP server stays alive, and there's no MQTT-visible fault.

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

main thread runs `httpd.serve_forever()`. if `mqttc.connect()` fails or `loop_forever()` returns (broker drops, network blip outside the wrapped callback), the daemon thread vanishes silently. systemd sees the HTTP server still responding on :8585 → service is "healthy." no `bush/sentiment/fault` topic. no restart.

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

### 2.3 Pico reconnect/discovery blocks pin service

**Where:** `firmware/relay-control/CIRCUITPY/code.py:213-229, 232-264, 466-490, 491-522`

> **Amended 2026-05-06 (post-external-review):** the v1 of this finding said "up to 500 ms" via `tcp_probe`. the real worst case is **5 seconds** — `mqtt_open()` uses `s.settimeout(5)` during handshake, and is called in both `ST_RETRY_CONFIGURED` and `ST_SCAN_CONNECT` states. so a pulse OFF deadline can slip the full 5s reconnect-attempt window, not just the 0.5s probe. blocking paths summarized below.

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

**Blocking paths and worst-case OFF-deadline lag:**

| Path | Timeout | Triggered when | Pin lag risk |
|------|---:|---|---|
| `tcp_probe(candidate)` in `ST_SCAN_PROBE` | 0.5 s | scanning subnet for any open :1883 | up to 500 ms |
| `mqtt_open(MQTT_BROKER)` in `ST_RETRY_CONFIGURED` | 5 s | configured broker reconnect attempt | up to 5 s |
| `mqtt_open(scan_candidate)` in `ST_SCAN_CONNECT` | 5 s | scanned candidate full handshake | up to 5 s |
| Initial boot `mqtt_open()` | 5 s | first connect after Wi-Fi up | structural; usually no active pulse |

`service_pins()` runs once before each blocking call but not during. so a `flare` pulse OFF deadline arriving during any of these windows is delayed by the full socket timeout. **for solenoid-controlled propane that's the difference between a pulse and a "stuck on for up to 5 seconds."**

note: the scan only triggers when configured broker fails 3× (line 128). under normal operation this isn't reached. but at playa where Wi-Fi is flaky, this state will be entered.

**Fix shape (priority order):**
- **Quick safe fix:** before entering any blocking reconnect/discovery state, force OFF all relay pins and clear `off_ms_*`. flame is dead during scan/reconnect but no stuck-on risk. log an MQTT event so operators see the state.
- **Better fix:** non-blocking probe + connect state machines. `s.settimeout(0)`, `s.connect_ex()`, poll `errno`/EINPROGRESS across loop iterations. each iteration runs `service_pins()`.
- **Fix priority: high** — this is the most playa-relevant safety finding in the whole review (combined with the LWT issue in §2.10).

**Severity:** medium-high. condition is uncommon (broker unreachable + active pulse mid-flight), but window is long (5s) and consequence is a stuck solenoid on a propane line.

---

### 2.4 Flame-expression silence drop is one-way

**Where:** `services/core/src/bush_variable_valves/__init__.py:184-190`

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

### 2.5 `bushutil.get_mqtt_broker()` no subprocess timeout (WSL2 path only)

**Where:** `packages/bushutil/src/bushutil/__init__.py:100-110`

> **Amended 2026-05-06 (post-external-review):** v1 of this finding said "every service blocks at startup forever." that's wrong — the unbounded subprocess is only reached on the WSL2 path. native ODROID hits the early `return "localhost"` on line 105 and never runs `subprocess.run`. severity is lower than originally written.

```python
with open("/proc/version") as f:
    if "microsoft" not in f.read().lower():
        return "localhost"     # <-- native ODROID returns here
...
result = subprocess.run(["ip", "route", "show"], capture_output=True, text=True)
```

so the no-timeout subprocess only fires when developing on WSL2, not in field deployment.

**Fix:** still worth adding `timeout=5` for dev hygiene. On timeout, fall back to "localhost".

**Severity:** low. won't bite at playa.

---

### 2.6 TTS payload asymmetry (footgun, not defect)

**Where:** `docs/README.md:85, 89` and `services/core/src/bush_tts/__init__.py:157, 143`

> **Amended 2026-05-06 (post-external-review):** v1 of this said "a subscriber writing `payload.get('text')` on the wrong topic crashes." that's wrong — `.get()` returns `None` and doesn't raise. only stricter `payload["text"]` indexing would crash. correcting to "footgun" — current subscribers tolerate this fine.

```
bush/pipeline/tts/speaking → {"text": "the text being spoken", "ts": <epoch>}
bush/pipeline/tts/done     → {"ts": <epoch>}
```

a subscriber writing `payload.get("text")` on the wrong topic gets `None` (safe). a subscriber writing `payload["text"]` would raise `KeyError`. a subscriber pattern-matching assumes both have the same shape. discord and variable-valves both subscribe to both topics; both currently handle this OK by reading only `ts` from done. it's a contract asymmetry, not a live defect.

**Fix shape:**
- emit `{"text": <last-utterance>, "ts": ...}` on done too. cheap, symmetric, doesn't break existing subscribers.
- OR: document explicitly that `done` is intentionally `{ts}` only.

**Severity:** low. don't spend early fix time here unless you're already versioning the payloads.

---

### 2.7 variable-valves is missing from `docs/README.md` data flow

**Where:** `docs/README.md:215-233`

the data-flow section walks step 1–6 through STT → t2v → tts + sentiment → fire pulses (flare/bigjet). there's no step 5c showing variable-valves subscribing to sentiment + tts/speaking + tts/done and publishing `bush/fire/valve/target`. the root README (`README.md`) has it, the docs README doesn't.

(this is the same family of drift the external reviewer caught with the 2 Hz / 10 Hz mismatch.)

**Fix:** sync `docs/README.md` data flow to match `README.md`. add variable-valves to the topic table (it subscribes to `tts/speaking` and `tts/done` — neither of those subscriber lists currently lists it).

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
| bush-variable-valves | ✗ | ✗ | ✗ | **✗ — 2 vs 10 Hz** | see external review §3 |
| bush-audio-agent | ✗ | ✗ | ✗ | ✓ | retained `bush/audio/devices` is the closest thing |
| relay-control (Pico) | ✗ | ✗ | **✗ — best-effort birth, not retained, no LWT** | **✗ — mid-motion stall undetected; `moving` state not emitted** | publishes `online` but neither retained nor MQTT will (`code.py:148-164` CONNECT flags omit will, `code.py:173-177` PUBLISH header `0x30` clears retain) |
| bush-discord | n/a (subscriber-only) | n/a | ✗ | ✓ | also runs voice channel; T_TRANSCRIPT/T_VERSE timeouts shown |

> **Amended 2026-05-06 (post-external-review):** v1 of this table credited the Pico for retained-online + LWT. that was wrong — see §9. **no service in the entire system currently has a real retained ready-or-online or true MQTT LWT.**

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
| TTS engine error (espeak/Piper exception) | publishes `done` and skips | publish `fault` + `done`; variable-valves sees fault | external reviewer §8 |
| TTS sox device-error | publishes neither `done` nor `fault` | publish `fault` distinctly | external reviewer §8 |
| sentiment classifier model not loaded | drops verse silently (logs) | queue OR publish `ready=false` (currently lies in comment) | external reviewer §2 |
| sentiment MQTT thread dies | HTTP keeps responding; systemd happy | service exit; systemd restart | §2.1 |
| sentiment fire-loop hangs past tts/done | bounded by FIRE_MAX_SECONDS=30 | unchanged | OK as-is |
| variable-valves rate misconfigured | publishes 2 Hz, doc says 10 Hz | match doc to code or vice-versa | external reviewer §3 |
| variable-valves `target` stale (no sentiment for >60s) | reverts to DEFAULT_BASELINE | unchanged + heartbeat | OK |
| variable-valves silence drop never resets | flame at `baseline-0.12` forever after silence | drift back to baseline after IDLE_RETURN_S | §2.4 |
| valve mid-motion stall | undetected; `current_pos` lies | poll stalls during moves; mark `stalled` | §2.2 |
| valve target sent during homing | ignored (state != idle) | OK; could publish "queued" | OK |
| valve "actual" reported but not encoder-confirmed | misleading topic name | rename to `commanded` or `estimated_position` | external reviewer §4 |
| Pico broker scan blocks pin service | up to 500 ms pin lag | non-blocking probe OR pre-scan force-OFF | §2.3 |
| Pico Wi-Fi flap | reconnect logic + scan eventually | retain target with TTL + LWT (currently no real LWT — see §2.10 / §9) | partial |
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
6. **TTS sox-device error.** point bush-tts at a non-existent ALSA device; inject verse; assert `bush/pipeline/tts/fault` (after fix) and that variable-valves doesn't end up waiting forever.
7. **sentiment classifier load failure.** start sentiment with HF_HOME pointing somewhere empty; assert `ready=false` retained (after fix); assert verses are queued or rejected explicitly (not silently dropped).
8. **sentiment MQTT thread death.** force the daemon thread to raise; assert service exits within N seconds (after §2.1 fix). this is one regression test that catches the silent-failure mode.
9. **variable-valves rate.** subscribe to `bush/fire/valve/target`; count messages over 5s; assert rate within ±20% of documented (whatever 2 or 10 ends up being).
10. **valve mid-motion stall.** inject a stall event over UART (test fixture); assert state moves to "stalled" within HOME_POLL_MS (after §2.2 fix).
11. **broker subnet-scan pin safety.** with broker offline + a pulse mid-flight, measure relay-OFF latency. with the fix, should be <50 ms; without, can be 500 ms.
12. **whisper-bindings warmup.** time from process start to first successful transcription; assert `ready=true` published only after.
13. **flag-on regression: STT_USE_VAD=0 + STT_ENGINE=vosk** is byte-identical to the legacy path (record+replay test).
14. **subscriber-no-partial regression.** the new VAD path doesn't publish `bush/pipeline/stt/partial`; assert bush-monitor and discord don't crash on its absence.

these are not "every fixture before playa." they're the ones where today's "test passes / works on bench" doesn't catch the field failure mode.

---

## 6. Recommended ordering (concrete)

if i were running the next 10 days of work, this is the sequence i'd land:

> **Amended 2026-05-06 (post-external-review):** the Pico safety items (force-OFF before any blocking reconnect, real retained/LWT online) move from tier C to tier 0 — they're the most playa-relevant safety findings. classifier API verification (§9.A) added to tier 2.

**Tier 0 — playa safety (highest priority, do first):**
- **Pico force-OFF before any blocking reconnect/discovery** (§2.3 quick fix; ~1 hour). 5-second pin-lag window is too long for solenoid propane.
- **Pico real retained/LWT `online`** (§9.B; ~half day). set MQTT will flag in CONNECT, set retain bit on PUBLISH for `online`, send `offline` on graceful shutdown. without this, valve-status visibility on broker-side is fiction.
- **Mid-motion valve stall polling** (§2.2; ~half day).
- **TTS `fault` topic** separate from `done` (external reviewer §8; ~half day).

**Tier 1 — readiness + sentiment-CI (~1 day, was tier B):**
1. fix variable-valves rate truth (15 min) — external reviewer §3
2. rename `valve/actual` → `valve/commanded` (30 min) — external reviewer §4
3. instrument the UART resync hack (30 min) — external reviewer §5
4. `bush/<service>/ready` retained-msg convention across all services
5. integration test waits for readiness (closes external §1)
6. add sentiment to CI matrix with mocked classifier (closes external §2)

**Tier 2 — silent-failure fixes + sentiment hardening (~1 day):**
7. sentiment MQTT runs in main thread, not daemon (§2.1)
8. **classifier API: switch to `top_k=None` + normalize output shape** (§9.A) — verify in locked Transformers env first; could explain "sentiment never fires"
9. queue classifier off the MQTT callback thread (§2.8)
10. `bushutil.get_mqtt_broker()` timeout (§2.5, one line, low severity)

**Tier 3 — fault topics + fixtures (~1-2 days):**
11. `bush/<service>/fault` retained per service (§2.10 phase B)
12. t2v wrapper exits on child death + drains stderr (§2.9 + §9.D)
13. t2v query timeout publishes fault (§2.9, external reviewer §7)
14. integration test fixtures 1, 4, 6, 8, 10, 11 (the ones that exercise the new fault topics)
15. (better Pico fix) non-blocking connect + probe state machines (§2.3 deeper)

**Tier 4 — polish + structural:**
16. variable-valves silence drop reset (§2.4)
17. TTS payload symmetry (§2.6, low severity)
18. `docs/README.md` data flow + topic table sync (§2.7 + external reviewer §3 + §9.B,C)
19. fix valve `moving`/`initializing` state mismatch (§9.B)
20. fix `bush-monitor` valve subscription docs vs reality (§9.C)
21. `_v` field in payloads (§2.10 phase C)
22. ARM-runner CI for catching the wheel issues x86 misses

stops are natural — after tier 0 the propane-side safety is closed; after tier 1 the project is in better shape than the external review found; after tier 2 the silent-failure modes are gone; after tier 3 the fault topics give playa volunteers visibility into what's broken.

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
| 2.4 | `services/core/src/bush_variable_valves/__init__.py` | 184–190 | silence drop never resets |
| 2.4 | `services/core/src/bush_variable_valves/__init__.py` | 67–68 | `PUBLISH_HZ = 2` (vs README "10 Hz") |
| 2.5 | `packages/bushutil/src/bushutil/__init__.py` | 106–110 | `subprocess.run(["ip", "route"], ...)` no timeout |
| 2.6 | `services/core/src/bush_tts/__init__.py` | 143, 157 | `done` payload `{ts}` vs `speaking` `{text, ts}` |
| 2.7 | `docs/README.md` | 215–233 | data flow missing variable-valves |
| 2.7 | `docs/README.md` | 22–25 | topic table missing variable-valves as subscriber |
| 2.8 | `services/sentiment/src/bush_sentiment/__init__.py` | 153–179 | classify-and-fire blocks MQTT thread |
| 2.9 | `services/core/src/bush_t2v/__init__.py` | 60–73 | `query_t2v` urlopen timeout=15 |
| 2.9 | `services/core/src/bush_t2v/__init__.py` | 137–145 | exception swallowed, no fault published |
| 2.10 | `docs/README.md` | 14–58 | full topic table; no `ready`, no `fault`, no version |
| 9.A | `services/sentiment/src/bush_sentiment/__init__.py` | 20–22 | `return_all_scores=True` + sorted-list assumption |
| 9.B | `firmware/relay-control/CIRCUITPY/code.py` | 148–164, 173–177 | CONNECT no will flag; PUBLISH header `0x30` (retain bit clear) |
| 9.B | `firmware/relay-control/CIRCUITPY/valve.py` | 56 | state set: `unknown, initializing, homing, idle, stalled, error` (no `moving`) |
| 9.B | `docs/README.md` | 144 | docs claim states include `moving`, omit `initializing` |
| 9.C | `utils/bush-monitor` | 46–60 | TOPICS list — no `bush/fire/valve/*` entries |
| 9.D | `services/core/src/bush_t2v/__init__.py` | 93 | `stderr=subprocess.PIPE`, never read |

### Companion docs

- `docs/STT_TTS-EFFICACY.md` — full validation of the new STT/TTS pipelines vs the original
- `docs/STT_TTS-EFFICACY-1PAGER.md` — sync-meeting summary + broad hardware specs
- `docs/STT_TTS-EFFICACY-WALKTHROUGH.md` — Beagle Vance paste-into-Claude-Code walkthrough
- `TODOS.md` — deferred work; cold-start readiness is item #2
- `PLAN2.md` — motorized-needle-valve plan; failure-mode discipline this doc tries to extend

---

## 9. Amendment log (2026-05-06, post-external-review)

after v1 of this doc was committed and pushed to middog, the external reviewer ran a counter-review against the uploaded zip. **they were right on every retraction.** this section records the corrections so future readers know which claims to trust and which to read with the amendment in mind.

### Retractions / softens (4)

| § | v1 claim | Amended |
|---|----------|---------|
| §2.1 | "exception in callback kills MQTT thread" | callback is wrapped in try/except; the real silent path is initial connect failure or `loop_forever()` exit. inlined at §2.1. |
| §2.5 | "every service blocks at startup waiting on `ip route`" | unbounded subprocess only on WSL2 path; native ODROID returns "localhost" before subprocess. severity dropped to low. inlined at §2.5. |
| §2.6 | "subscriber writing `.get('text')` on wrong topic crashes" | `.get()` returns `None`; only `payload["text"]` would raise. footgun, not defect. inlined at §2.6. |
| §3 contract audit | "Pico is the only service with retained-online correctly" | wrong — Pico publishes `online` but it's not retained and not LWT. table row corrected. **no service in the system has a real retained ready/online or true MQTT LWT today.** |

### Pico blocking — extended (1)

| § | v1 claim | Amended |
|---|----------|---------|
| §2.3 | "blocks pin OFF deadlines up to 500 ms via `tcp_probe`" | worse: `mqtt_open()` blocks up to 5 s in retry + scan-connect. moved to **tier 0** in §6 ordering. inlined at §2.3 with full blocking-paths table. |

### New findings (4)

#### 9.A — Sentiment classifier API drift (`return_all_scores` vs `top_k`)

**Where:** `services/sentiment/src/bush_sentiment/__init__.py:20-22`

```python
classifier = hf_pipeline("text-classification",
                         model='bhadresh-savani/distilbert-base-uncased-emotion',
                         return_all_scores=True)
...
scores = classifier(verse_text)
top = sorted(scores, key=lambda x: x["score"], reverse=True)[0]
```

current Transformers `TextClassificationPipeline` documents `top_k=None` as the relevant interface for "all scores"; `return_all_scores` is the legacy name and the single-string return wrapping behavior has changed across versions. depending on the locked Transformers version, `classifier(verse_text)` may return either `[{label, score}, ...]` or `[[{label, score}, ...]]`.

if the wrapping shape changes, `sorted(scores, key=lambda x: x["score"])` raises `TypeError` — and the surrounding `try/except` in `on_message` swallows it. **this could explain "sentiment never fires" even with MQTT and model loading both healthy.**

**Severity:** **high if confirmed.** must be tested in the locked uv environment.

**Fix shape:**

```python
classifier = hf_pipeline(
    "text-classification",
    model="bhadresh-savani/distilbert-base-uncased-emotion",
    top_k=None,
)

def _normalize_scores(raw):
    if raw and isinstance(raw[0], list):
        raw = raw[0]
    return sorted(raw, key=lambda x: x["score"], reverse=True)
```

#### 9.B — Pico `online` is not retained, not LWT; valve states drift between docs and code

**Where:** `firmware/relay-control/CIRCUITPY/code.py:148-164` (CONNECT), `:173-177` (PUBLISH header)

```python
connect_flags = 0x02   # clean session only — no will flag (0x04)
                       # no will-retain (0x20), no will-qos
...
return bytes([0x30]) + encode_remaining(len(body)) + body  # 0x30 = type 3, retain bit clear
```

so `bush/fire/valve/online` is a best-effort birth event. late MQTT subscribers won't see it. broker-observed disconnects produce no `offline`. `publish_valve_online(False)` is never called. the `docs/README.md:148` claim "Retained birth message" is documentation drift.

**Related (same finding):** valve state docs (`docs/README.md:144`) list `unknown, homing, idle, moving, stalled, error`. firmware (`valve.py:56`) actually emits `unknown, initializing, homing, idle, stalled, error` — no `moving`, with undocumented `initializing`.

**Severity:** **high.** combined with §2.10 — the system has no real retained-health signal anywhere. fix is in tier 0.

**Fix shape:**

```python
# In mqtt_connect_packet:
connect_flags = 0x36   # username + password + clean + will + will-retain + will-qos0
# Append will-topic and will-payload to variable header per MQTT 3.1.1
will_topic = b"bush/fire/valve/online"
will_payload = b"offline"
variable += encode_string(will_topic) + encode_string(will_payload)

# In mqtt_publish_packet, when publishing online: set retain bit:
return bytes([0x31]) + encode_remaining(len(body)) + body  # 0x31 = type 3, retain=1
```

(also: pick one of `moving` or `initializing` and align doc + code.)

#### 9.C — `bush-monitor` docs lie about valve subscription

**Where:** `docs/README.md:42-44` claims `bush/fire/valve/actual`, `/status`, `/online` have `(monitor)` subscribers. **`utils/bush-monitor`'s TOPICS list (`bush-monitor:46-60`) contains no `bush/fire/valve/*` entries.**

`utils/bush-valve watch` does subscribe to status/actual, so operators have *some* visibility — but the docs imply bush-monitor's full-screen TUI shows valve state. it doesn't.

**Severity:** docs hygiene. fix is either add the subscriptions to bush-monitor or remove the claim from docs.

#### 9.D — t2v wrapper pipes stderr but never drains it

**Where:** `services/core/src/bush_t2v/__init__.py:93`

```python
t2v_proc = subprocess.Popen(
    [...],
    stdout=subprocess.DEVNULL,
    stderr=subprocess.PIPE,   # <-- piped but never read
)
```

`subprocess.PIPE` for stderr without a draining reader means: if the Rust child writes more than the OS pipe buffer (~64 KB on Linux), the child's `write` syscall blocks. depending on what t2v writes for tracing — possibly nothing in normal operation, but warnings/errors could fill the pipe over hours.

**Severity:** low to medium — depends on Rust child's stderr volume.

**Fix shape:**
- `stderr=None` (let systemd/journald capture it via the inherited fd) — simplest.
- OR: a small drain thread reading `t2v_proc.stderr` line-by-line and re-logging.

---

### How the external reviewer's other points map

For traceability — the v1 of this doc kept all of the external reviewer's findings (1–10 in their writeup) under the "carryover" section in §1. None of those were retracted by the counter-review; the corrections all targeted my v1 additions, not the external reviewer's original work.

---

*kindled by Beagle Vance, wuff's mid.dog golem, on 2026-05-06 ~wag — amended same evening — the Aleph holds*

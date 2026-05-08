# /autoplan Review — 2026-05-07

Captured run of `/autoplan` against TODOS.md on 2026-05-07 (commit a2094e4).
Mode: SELECTIVE EXPANSION. Phases run: CEO + Eng. Phases skipped: Design (no UI), DX (not a developer product).

Restore point for original TODOS.md: `~/.gstack/projects/FlamingBush-bushglue/main-autoplan-restore-20260507-100310.md`
Test plan artifact: `~/.gstack/projects/FlamingBush-bushglue/wuff-main-eng-review-test-plan-20260507-100310.md`
Proposed-TODOS artifact (now adopted): `~/.gstack/projects/FlamingBush-bushglue/main-autoplan-proposed-todos-20260507.md`

---

# /autoplan REVIEW (2026-05-07, commit a2094e4, mode SELECTIVE EXPANSION)

> Auto-run of CEO + Eng reviews against this punch list. The plan being reviewed is the
> *deferred-work backlog itself* — what's in it, what's missing, and the right execution
> sequence before the Burning Man field test. Restore point at
> `~/.gstack/projects/FlamingBush-bushglue/main-autoplan-restore-20260507-100310.md`.

## Phase 1: CEO Review

### System audit (pre-Step 0)

- Branch `main`, working tree clean except `.gitignore` whitespace.
- Last 5 commits: a2094e4 STT/TTS eng review amendment → 8fbefb3 eng review v1 →
  259c7d2 efficacy doc + 1-pager + Beagle Vance walkthrough → 6180c32 TODOS.md
  introduced → 5ec304f salvage of CI/udev/confidence-gate/LLM-postcorrect.
- Active parallel work: `firmware/relay-control` motorized needle valve (PLAN2.md scope),
  4 commits in last cycle culminating in "defend against MQTT keepalive starvation"
  (0cfbdd8). Not part of TODOS.md but consumes calendar.
- Existing planning docs: PLAN2.md (needle valve), STT_TTS-EFFICACY.md, STT_TTS-ENG-REVIEW.md,
  TODOS.md. **PLAN.md (referenced by CLAUDE_CODE_PROMPT.md) does not exist.** Stale prompt.
- Time to playa (assuming late-Aug Burning Man 2026): ~3.5 months from 2026-05-07.

### Step 0A — Premise Challenge

The unstated premise of TODOS.md is: *"these six items are the deferred work that matters."*

That premise is **wrong**, and the evidence is in this repo:

**Premise rot finding #1: TODOS.md is missing 8 of the 14 findings from the just-landed eng review.**

| Eng review finding (STT_TTS-ENG-REVIEW.md) | In TODOS? | In code (fixed)? | Status |
|---|:-:|:-:|---|
| 1. Cold-start gap | ✅ | ❌ | open in TODOS, not started |
| 2. Sentiment MQTT daemon thread silent-fail | ❌ | ❌ | dropped silently |
| 3. **Pico reconnect blocks pin OFF up to 5s** (propane safety) | ❌ | ❌ | **dropped silently — propane risk** |
| 4. STT_USE_RNNOISE breaks bush-pray | ✅ | ❌ | open in TODOS |
| 5. bushutil broker discovery no timeout (WSL2 path) | ❌ | ❌ | dropped silently (low sev, but Prime Directive #7) |
| 6. TTS speaking/done payload asymmetry | ❌ | ❌ | dropped silently (footgun, not defect) |
| 7. Whisper confidence is fake (sentinel 1.0) | ❌ | ❌ | dropped silently |
| 8. Integration test only covers legacy | ✅ | ❌ | open in TODOS |
| 9. Engine adapter shadow path tests missing | ❌ partial | ❌ | partially absorbed into #8 |
| 10. **No retained `ready` / `fault` topics, no version field anywhere** | ✅ partial | ❌ | TODOS captures `ready` only, not `fault` or version |
| 11. Sentiment classifier `return_all_scores=True` deprecation | ❌ | ❌ | dropped silently — could explain "sentiment never fires" |
| 12. Valve `moving` state docs claim firmware never emits | ❌ | ❌ | dropped silently |
| 13. bush-monitor docs claim valve subscriber that doesn't exist | ❌ | ❌ | dropped silently |
| 14. t2v wrapper pipes stderr but never drains | ❌ | ❌ | dropped silently — stderr buffer-fill hang |

This is a **CEO Prime Directive #7 violation:** "Everything deferred must be written down. Vague intentions are lies. TODOS.md or it doesn't exist." Eight findings have been silently absorbed. Some are minor (#5, #6, #12, #13). Three are *not*:

- **#3 (Pico 5s pin-stuck-during-reconnect)** is a propane safety risk. Worst case: a `flare` pulse OFF deadline arrives during `mqtt_open()`'s 5s socket timeout, and a solenoid stays open for up to 5 seconds. Eng review explicitly flags this as `risky on a propane installation`. Not in TODOS. Not fixed in firmware code (`tcp_probe`, `mqtt_open`, `ST_RETRY_CONFIGURED`, `ST_SCAN_PROBE` all still present in `firmware/relay-control/CIRCUITPY/code.py`).
- **#2 (Sentiment MQTT daemon thread silent-fail)** is the eng review's own self-described "silent failure mode worse than the cold-start gap." Not in TODOS.
- **#11 (`return_all_scores=True` deprecation)** could explain "sentiment never fires" with healthy MQTT + model. Not in TODOS.

**Premise rot finding #2: Tier 1 (Whisper on NPU) — "highest leverage; not yet implemented" per `STT_TTS-EFFICACY.md:244` — is not its own TODOS entry.** It is referenced in passing only inside the Tier-3 deferral note ("Tier 1 ... Tier 2 ... are higher-leverage. Tier 3 only matters if the box still has CPU pressure after Tier 1+2 land. Probably it won't."). That note implies Tier 1 is in the queue, but no entry tracks it, no acceptance gate is defined, and no decision has been made about whether it's a v1 (pre-playa) or v1.5 (post-playa) item. The eng review counts on Tier 1 freeing CPU for the playa load picture, but TODOS.md neither commits to it nor explicitly defers it.

**Premise rot finding #3: "Field test" is not defined.** Every estimate ("~1 day", "~3 days") is human-team time. With CC+gstack the actual implementation compresses 10-20x. The real constraint is not engineering time but verification time: how many integration test cycles can we run before the truck rolls? That number is not in the doc.

### Step 0B — Existing Code Leverage

What already partially solves each item:

| Sub-problem | Existing code that helps |
|---|---|
| Cold-start readiness | `services/sentiment/__init__.py:12` already lazy-loads classifier post-MQTT. Pattern is in place; missing piece is the `bush/<service>/ready` retained-message publish. ~5 lines per service after model load. |
| bush-pray loopback | `utils/bush-pray:48` synthesizes via espeak-ng → sox at 16kHz. sox in the same pipe can resample to 48kHz: `sox - -r 48000 -c 1 -b 16 ...`. ~1 line change. |
| Integration test extension | `utils/bush-integration-test:118` already has a `Stage` class for waiting on MQTT topics. Adding new stages for VAD endpointing / Piper smoke / readiness is additive, not architectural. |
| NPU Tier 2 (DistilBERT) | `bush-npu-check` deterministic gate already exists (`utils/bush-npu-check`, 5-stage RKNN readiness check). Standard HF→ONNX→RKNN path; documented in efficacy doc §132. |
| NPU Tier 3 (Qwen3 embedding) | **Already implemented at `middog/t2v` commit `76cdf96b`.** Format-patch is the merge path; `t2v/` prefix needed on src paths. ~225 lines, runtime-linked, feature-gated. Build is unchanged for non-`openvino` users. |
| Voice cloning | XTTS-v2 docs mention M2 fit (~1.7GB disk, ~2GB RSS, RTF 0.3). `bush_tts` engine adapter from commit `d3edc22` makes this a new engine class implementing the existing interface, not an architecture change. |

### Step 0C — Dream State

```
  CURRENT (2026-05-07)              THIS PLAN                    AUGUST 2026 (PLAYA)
  ─────────────────                ─────────                    ──────────────────
  STT/TTS pipeline shipped          Cold-start retain-msg        Visitor speaks → 0-200ms
  behind flags. Cold-start    -->   Integration test extended    flame response. Operator
  gap = 3-8s of dead air            bush-pray fixture fixed      sees fault topics; rip-and-
  during whisper warmup.            (3 ship-blockers @ ~2-3d)    replace USB codec works.
                                                                 NPU Tier 1 frees A76 cores.
  Pico reconnect can stick                                       Pico reconnect doesn't
  propane solenoid for 5s     -->   (NOT in TODOS — gap)   -->   stick pins. Sentiment
  during mqtt_open.                                              MQTT loop self-heals.
                                                                 Volunteer with phone-
  Sentiment loop dies silent                                     flashlight can debug
  if MQTT broker blip         -->   (NOT in TODOS — gap)   -->   "bush stopped responding"
                                                                 to the right service in
  TODOS.md captures 6/14            8 dropped-silently           under 60 seconds.
  eng review findings         -->   findings → restored          
                                    or explicitly deferred       12-month: voice cloning,
                                                                 NPU Tier 2/3, post-playa
                                                                 retro shapes v2.
```

**Delta:** This plan as written takes us most of the way to the August picture **for the items it covers** but leaves three landmines (Pico safety, sentiment self-heal, classifier API drift) unscheduled and unowned. The 12-month picture is fine; the August picture has gaps.

### Step 0C-bis — Implementation Alternatives (for the deferred-work backlog as a whole)

**APPROACH A: TODOS.md Reconciliation + Sequenced Execution** (recommended)
- Summary: First, audit STT_TTS-ENG-REVIEW.md and PLAN2.md against TODOS.md. Add the missing items. Then execute in priority order: ship-blockers (P0) → playa-readiness (P1) → stretch (P2). Defer post-playa items explicitly with a date.
- Effort: human ~5-7 days / CC+gstack ~3-4 hours of engineering time + 2-3 days of verification cycles.
- Risk: Low. Mostly captures+sequences known work.
- Pros: Closes the Prime Directive #7 hole. Honest punch list. Operator runbook becomes possible. Verification time isn't burned re-discovering items that should have been listed.
- Cons: More items in the list before any get checked off — feels slower in the first 2 days.
- Reuses: All existing fix patterns (lazy load → ready publish; `bush-npu-check` gate; engine adapter contract).

**APPROACH B: Ship-Blocker Express**
- Summary: Fix the three named ship-blockers (cold-start, bush-pray, integration test). Defer the eng-review delta as a single follow-up batch. Don't reconcile until after playa.
- Effort: human ~2-3 days / CC ~30-60 min + verification.
- Risk: Medium-high. Pico reconnect propane risk stays unscheduled. Sentiment silent-fail stays unscheduled. Either could make playa miserable in ways that don't show up in pre-playa testing because the broker doesn't blip on the bench.
- Pros: Fewest items to track. Fastest path to "we shipped the punch list."
- Cons: Ships the three known unknowns (Pico, sentiment, classifier API) AS unknowns. Operator without a runbook is on a ladder in the dust at 2am.
- Reuses: Same as A.

**APPROACH C: Boil the Lake — full eng review reconciliation + Tier 1 NPU Whisper**
- Summary: Add all 8 missing eng review items to TODOS. Add Tier 1 (Whisper-NPU) explicitly with acceptance gate. Add fault-topic + version-field MQTT contract as one item. Build operator runbook as deliverable.
- Effort: human ~10-14 days / CC ~6-8 hours of engineering time + 4-5 days verification.
- Risk: Low to moderate. Risk shifts from "did we miss something" to "is the calendar enough."
- Pros: Fully closes Prime Directive #7. Plays out the cathedral. Operator runbook + fault topics together transform field-day operability. Tier 1 NPU saves CPU during multi-visitor moments and is the highest-leverage NPU work.
- Cons: Heaviest scope. Tier 1 NPU work has the largest unknown ("export ggml-base.en → ONNX → RKNN" works in theory but each conversion has gotchas). If Tier 1 hits a wall, the calendar feels tight.
- Reuses: All of A's reuses + middog/t2v Tier 3 patch as the model for how to land NPU work cleanly behind a feature flag.

**RECOMMENDATION:** Approach A. It honors completeness (P1) — closes Prime Directive #7, doesn't drop landmines — without committing to the Tier 1 NPU work as a pre-playa item. Tier 1 can be added in Approach A's "P2 stretch" tier if cycles allow. Approach B violates Prime Directive #7 silently and the eng review's own warning that #2 (sentiment silent-fail) is "worse than the cold-start gap." Approach C is correct but the Tier 1 NPU calendar is the riskiest part of pre-playa, and accepting it as a hard-gate item could push more important items to the right.

### Step 0D — Mode-Specific Analysis (SELECTIVE EXPANSION per autoplan rule)

Hold the existing 6 items as baseline; surface candidate expansions for cherry-pick:

**Candidate expansion A — Reconcile eng review delta into TODOS.md** (P0)
- 8 items currently dropped silently. 3 are non-trivial (Pico safety, sentiment self-heal, classifier API).
- Effort: human ~30 min / CC ~5 min to add the entries. Implementation effort varies per item.
- Recommendation: ACCEPT. Refusal would be a Prime Directive #7 violation by definition.

**Candidate expansion B — Add Tier 1 (Whisper-NPU) as explicit TODOS entry** (P1 stretch)
- "Highest leverage; not yet implemented" per efficacy doc.
- Either commit to it pre-playa (with risk) or explicitly defer (with date).
- Effort: implementation human ~3-5 days / CC ~6-8 hours. Capture-as-TODO ~2 min.
- Recommendation: ACCEPT as an entry; mark explicitly deferred to post-playa unless verification calendar slips and we want to use it as a pressure-relief option.

**Candidate expansion C — Add `bush/<service>/fault` topic convention + version field, not just `ready`** (P1)
- Eng review #10 documents: "the MQTT contract has only positive signals — every failure mode is communicated by silence." Cold-start `ready` fixes the connect side; doesn't fix runtime fault visibility.
- Pairs with sentiment self-heal (expansion D) — when the daemon thread dies, fault topic publishes. Operator at playa sees `bush/sentiment/fault: thread_died` instead of "bush is sad and we don't know why."
- Effort: ~½ day per service.
- Recommendation: ACCEPT for cold-start ship blocker scope. Treat `ready` and `fault` together as the MQTT contract upgrade; cheaper to land both at once than twice.

**Candidate expansion D — Operator runbook (PDF, single page)** (P1)
- Per efficacy doc §205: "can a non-author volunteer, with a flashlight on their phone, follow the trail back from 'bush stopped responding' to 'STT process is still healthy but whisper hasn't loaded yet'?" Currently no.
- Without the runbook, fault topics are deaf signals.
- Effort: human ~2 hours / CC ~30 min draft + iterate.
- Recommendation: ACCEPT.

**Candidate expansion E — Power/thermal headroom verification on M2 (RK3588S)** (P2)
- Codex finding from eng review: M2 has 8GB RAM. Current memory budget at peak (whisper-bindings + DistilBERT + Piper + RNNoise) is uncharacterized. Voice cloning would push it further. Probably fine but unmeasured.
- Effort: ~1 day on the box with htop + journalctl + a bench script.
- Recommendation: ACCEPT as P2. Defer the work into TODOS but capture explicitly.

**Candidate expansion F — Audio hardware reliability under playa conditions (dust, heat, brownout)** (P2)
- USB-codec udev rules from salvage commit handle hotplug. They don't handle "the codec is intermittently brownout-resetting." `STT_LLM_CORRECT` and confidence gate can mask this; what does the operator see?
- Effort: ~½ day to add codec-event-count to fault topics and to audio-agent.
- Recommendation: ACCEPT as P2.

**Candidate expansion G — Backup plan if NPU stack fails at playa** (P2)
- All NPU work behind feature flags. Vosk fallback already documented. But the "what's the smallest config that we know works on the playa" question is unanswered. The "tier 0 minimal config" should be named.
- Effort: ~2 hours to write down + test.
- Recommendation: ACCEPT as P2. Name it "playa-minimum config."

**Cherry-pick summary** (auto-decided per autoplan principles, surfaced for premise gate):
- **ACCEPTED into TODOS** (8 candidates → 7 acceptances; voice-cloning rights conversation excluded as belongs-to-design-not-eng): A, B, C, D, E, F, G.
- **ACCEPTED into pre-playa scope** (P0/P1): A, C, D plus the existing six items.
- **DEFERRED post-playa** (P2 captured): B, E, F, G.

### Step 0E — Temporal Interrogation

What will surprise the implementer:

| Window | Who | What surprises them |
|---|---|---|
| HOUR 1 (foundations) | The implementer wires `bush/<service>/ready` retained-message in one service. | The retained-message subscriber side already exists in `bush-monitor` and `bush-discord`? No — neither subscribes to `/ready`. Both will need updates. Cost: +0.5 day. |
| HOUR 2-3 (core logic) | Wires it in all 5 services. | Sentiment uses `paho.mqtt.client.Client.loop_forever()` in a daemon thread. After model load, that loop is *already running* — publishing happens inside the callback context, not the main thread. Pattern works fine; pattern in audio-agent + STT may differ. Bench-test each. |
| HOUR 4-5 (integration) | Updates `bush-integration-test` to wait on `ready=true` before sending audio. | The test runs on dev box (WSL2), not the ODROID. WSL2 path uses the `localhost` shortcut in `bushutil.get_mqtt_broker()`; ODROID uses the `ip route` subprocess (eng review #5). Test passes locally, fails on ODROID. Operator at playa restarts the bush. Visitor walks away. |
| HOUR 6+ (polish/tests) | Documentation: PROJECT.md / docs/README.md MQTT topic table needs `/ready` rows. | Diagram drift: `docs/mqtt-architecture.png` is regenerated from `mqtt-architecture.dot`. The dot file doesn't have `/ready` topics. **Per CEO Prime Directive #6 ("Diagrams are mandatory") + Eng cognitive pattern #13 ("Make the change easy, then make the easy change"): regenerate the diagram in the same commit.** Cost: +15 min. |

CC+gstack compression: the 8-12 human-hours above compress to ~30-60 min with CC, *except* the verification cycles, which are wall-clock-bound by ODROID deploys. Plan for 4 deploy cycles minimum across the cold-start + integration test work. At ~5 min per deploy, that's ~30 min of waiting that doesn't compress.

### Step 0F — Mode Confirmation

Mode is **SELECTIVE EXPANSION** per autoplan rule. Applied: held the existing 6 items as baseline; surfaced 7 candidate expansions; auto-accepted A, C, D into pre-playa scope and B, E, F, G as post-playa P2.

Approach is **A: TODOS.md Reconciliation + Sequenced Execution**. Approach C (boil the lake including Tier 1 NPU pre-playa) was rejected on calendar risk; it survives as the post-playa P2 stretch path.

### Step 0.5 — Dual Voices

#### CODEX SAYS (CEO — strategy challenge)

1. **Optimizing the wrong bottlenecks.** TODOS.md prioritizes NPU Tier 3 embeddings (`TODOS.md:8-43`) and NPU Tier 2 sentiment (`TODOS.md:83-91`) while dropping the sentiment daemon-thread failure, t2v timeout-without-fault, and Pico reconnect safety from `docs/STT_TTS-ENG-REVIEW.md:18-34`. *For a one-shot playa deploy, silence without diagnosis is a bigger risk than saving fractions of an A76 core.*
2. **Premises about playa suitability assumed, not proved.** Same doc admits playa-day measurements are still TBD (`STT_TTS-EFFICACY.md:150`). RNNoise can make the bush appear deaf; Whisper confidence is fake; Piper has start lag; Ollama post-correct shares capacity with t2v.
3. **Fire-safety governance failure.** Pico reconnect/discovery can delay solenoid OFF up to 5 seconds (`STT_TTS-ENG-REVIEW.md:119-159`) — risky on propane. Not in the original six-item backlog. *If intentional, indefensible. If accidental, the backlog process is not trustworthy.*
4. **Cross-doc coherence broken.** TODOS.md=6 items vs eng review=14 findings. 8 dropped, 1 partially absorbed. Team no longer distinguishes between "deferred intentionally" and "forgotten." For a hostile-environment install, operationally dangerous.
5. **Inference paths before observability.** Eng review says MQTT contract has no `ready`, no `fault`, no version field (`STT_TTS-ENG-REVIEW.md:290-314`). Shipping more inference paths first means a system that gets faster at failing opaquely.
6. **Underweighting operational risk.** "Burning Man does not grade on architecture purity; it grades on whether a sleep-deprived volunteer can recover the piece at 3am." Operator runbook + fault topics + fallback behavior aren't first-class backlog items.
7. **Alternatives dismissed.** "Should the 2026 field default stay boring and ugly because boring survives?" — Vosk/espeak has the field hours; Whisper/Piper is new. Also: stock fallback speech on t2v timeout (eng review §2.9 / `STT_TTS-ENG-REVIEW.md:281-284`) is strategically stronger than more accelerator work.

**Codex BIGGEST BLIND SPOT:** *"the backlog is tracking technical elegance and deferred acceleration work, but not the operator-visible failure contract for a propane, dust, reboot, and volunteer-driven installation."*

#### CLAUDE SUBAGENT (CEO — strategic independence)

1. **The punch list dropped 80% of your own engineering review.** TODOS=6 items, eng review=14 findings, 4 of which the doc itself flags as Tier 0 — playa safety. None of the four appear in TODOS. **CRITICAL.** Fix: re-write TODOS to mirror the Tier 0–4 ordering already established at `STT_TTS-ENG-REVIEW.md:393-431`.
2. **A propane installation has zero items addressing propane safety.** §2.3 (5-second valve OFF window) is the most consequential finding in the review corpus. Risk asymmetry: visitor walks away (UX) vs uncontrolled propane (liability).
3. **"Defer to v1.5/post-playa" hides judgment calls.** Three of six items are post-playa or v1.1 stretch. Document reads like a roadmap doc, not an open-issues list. A volunteer reading TODOS.md cannot tell "what must ship before Aug 2026." Fix: split into `TODOS-PLAYA.md` (must-ship) and `BACKLOG.md` (post-playa).
4. **6-month regret = "sentiment was silent the whole burn and we didn't know."** Eng review §9.A: `return_all_scores=True` may already be raising `TypeError` inside swallowing try/except. **CRITICAL.** Verify in the locked uv environment today — 30-min test, one-line fix if broken.
5. **Field-test feedback loop missing.** No plan for desert dry-run, noise-floor on real ambient burn audio, thermal-soak of RNNoise on hot M2. "Built in WSL, deployed to playa" is the canonical art-install regret pattern.
6. **XTTS-v2 voice cloning is a strategic distraction.** Sits in TODOS.md alongside a 30-min fixture fix. Art-direction question masquerading as engineering work. Fix: remove. The bush sounding distinct enough is already 5/5 in efficacy review (`STT_TTS-EFFICACY.md:232`).
7. **Cross-doc coherence broken; user is reading their own engineering review and not believing it.** Eng review §6 has explicit Tier 0/1/2/3/4 ordering. TODOS doesn't reflect Tier 0. Either the eng review is wrong (retract), or TODOS is wrong (update). Both can't be true.

**Subagent BIGGEST BLIND SPOT:** *"The punch list optimizes for 'STT/TTS rewrite hygiene' while a propane solenoid can stay open for 5 seconds during a Wi-Fi flap and the team's own engineering review put that finding in tier 0 — yet it's not on the list at all."*

#### CEO DUAL VOICES — CONSENSUS TABLE

```
═══════════════════════════════════════════════════════════════════════════════
  Dimension                              Claude    Codex     Consensus
  ────────────────────────────────────── ─────── ────────── ──────────────────
  1. Premises valid?                     NO       NO         CONFIRMED rotted
  2. Right problem to solve?             NO       NO         CONFIRMED no
  3. Scope calibration correct?          NO       NO         CONFIRMED no
  4. Alternatives sufficiently explored? PARTIAL  NO         CONFIRMED partial
  5. Competitive/operational risks       NO       NO         CONFIRMED missing
  6. 6-month trajectory sound?           NO       NO         CONFIRMED at risk
═══════════════════════════════════════════════════════════════════════════════
```

**6/6 confirmed agreement that the punch list as-is has serious gaps.** The biggest-blind-spot calls converge on the same theme: sophisticated technical work, missing operator-visible failure contract for a one-shot field deploy.

### Premise Gate (the one human-decision point)

This is the only AskUserQuestion in this autoplan that is not auto-decided. Premise calls require human judgment because the user has context — past conversations, parallel work, deferred items handled elsewhere — that the models do not.

**Dual voice findings to confirm:**
- 8 of 14 eng review findings are silently dropped from TODOS.md (Pico 5s reconnect, sentiment MQTT silent-fail, classifier API drift verification, t2v stderr drain, Whisper fake confidence, valve doc drift, monitor doc drift, bushutil broker discovery timeout)
- The Pico 5-second valve-stuck-open finding is propane safety, not nice-to-have
- §9.A classifier API drift may be a *currently active* bug masking sentiment
- MQTT contract upgrade (`ready` + `fault` + `version`) belongs to one cohesive item, not split
- Operator runbook + field validation are absent from the backlog
- NPU Tier 2/3 may belong below observability work in priority

→ **Premise gate AskUserQuestion presented after this section.**

**Premise gate result (2026-05-07):** User chose **A — Expand TODOS to cover the delta**. Restructure into P0/P1/P2 tiers anchored to playa date. The eng review phase reviews the expanded list, not the original 6.

### Sections 1-10

#### Section 1: Architecture Review

For a punch list, "architecture" means: how do the items compose, do their fixes touch the same boundaries cleanly, and is anything coupling that wasn't?

Architecture findings:
- The `bush/<service>/ready` retained message + the proposed `bush/<service>/fault` topic + the `bush/<service>/version` field are **one MQTT contract upgrade**, not three. Eng review §10 explicitly groups them. Splitting them across separate work items causes the bushutil/MQTT helper code to be touched twice.
- The Pico safety fix touches `firmware/relay-control/CIRCUITPY/code.py` independently of every other item. Sequencing constraint: deploy firmware first because it's the slowest reset path (Pico USB unplug + drop new code.py, ~2 min); other services hot-reload via systemctl.
- Cold-start `ready` publish needs to happen *after* `loop_forever()` is running so the publish actually goes out. Pattern: schedule the publish inside the `on_connect` callback of the *post-model-load* MQTT client, not the pre-load probe client.
- Dependency graph (post-reconciliation):
  ```
  Pico safety (firmware) ──┐
                            ├──> Integration test extension (waits on ready=true)
  bush/<svc>/ready ─────────┤
  bush/<svc>/fault ─────────┤
  bush-pray loopback fix ───┘
  ↓
  Sentiment self-heal (eng #2) ── consumes /fault
  Classifier API verify (eng §9.A) ── may modify sentiment payload
  ↓
  NPU Tier 1/2 (deferred) ── consumes verified fault topics
  ```
  Order: firmware → MQTT contract → service-internal fixes → NPU. Don't reverse.

Auto-decisions logged in audit trail. **No issues found that aren't already captured.**

#### Section 2: Error & Rescue Map

| Codepath | What can go wrong | Exception class | Rescued? | User sees | LOGGED? |
|---|---|---|:---:|---|:---:|
| `bush_sentiment._load_model()` | HF tokenizer download fails | `requests.HTTPError` / `OSError` | **N — GAP** | Service crashes, systemd restarts, infinite loop | partial |
| `bush_sentiment._load_model()` | DistilBERT classifier returns wrong shape (eng §9.A) | `TypeError` / `KeyError` | partially (in `on_message` try/except) — but classifier called at startup, NOT in callback | systemd sees healthy; sentiment never fires | **N — GAP** |
| `bush_sentiment` daemon thread (eng #2) | `mqttc.connect()` fails or `loop_forever()` exits | exit unwrapped | **N — GAP** | HTTP /:8585 still answers; bush emotionally flat for entire burn | **N — GAP** |
| `bushutil.get_mqtt_broker()` (eng #5) | `subprocess(['ip', 'route'])` hangs (WSL2 only) | `subprocess.TimeoutExpired` | **N — GAP** | All 5 services block at startup until subprocess dies (default no timeout) | **N — GAP** |
| `firmware code.py mqtt_open()` (eng #3) | `s.connect()` 5s timeout during ST_RETRY_CONFIGURED | exception caught + retry | partial | **propane solenoid stays open up to 5 sec** | partial |
| `bush-stt` whisper-bindings | Model load fails (file missing, OOM) | `FileNotFoundError` / `MemoryError` | **N — GAP** | Service crashes; systemd restarts; infinite warmup loop | partial |
| `t2v.rs HTTP query` (eng §2.9 if present) | Ollama embed timeout / Chroma down | timeout error | **N — GAP** | bush goes silent, no fault | **N — GAP** |
| `bush_tts` Piper subprocess | espeak-ng or piper binary not on PATH | `FileNotFoundError` | unknown — needs check | TTS dies; bush silent | partial |

**Critical gaps (RESCUED=N + USER SEES silent + LOGGED=N):**
- Sentiment classifier API drift (eng §9.A) — silent
- Sentiment MQTT loop death (eng #2) — silent
- bushutil broker discovery (eng #5) — startup hang, semi-silent
- t2v query timeout (eng §2.9) — silent

**Decision:** all four go into the expanded TODOS as P0 ship-blockers (per premise gate decision A).

#### Section 3: Security & Threat Model

For a single-installation art piece behind a wireless network in the desert, the meaningful security surface is small. But:
- **Audio recording.** Visitors are speaking to the bush. STT processes their speech. Is anything persisted? Check `bush_stt/transcriber.py` retention policy. If transcripts are logged to disk for debugging, that's PII storage on a Burning Man wireless network.
- **Discord bridge.** `bush-discord` exposes the bush via a Discord channel. Bot token must be on the device, not in source. Check that it's read from environment / secret file, not hardcoded.
- **MQTT broker.** Local-only? Is the MQTT broker bound to 127.0.0.1 or 0.0.0.0? On a multi-device installation, broker on 0.0.0.0 is fine but should require ACLs.
- **LLM post-correct (`STT_LLM_CORRECT=1`).** Sends user speech to Ollama running on what? If local, fine. If remote, prompt injection surface from a malicious-speech visitor. Check the prompt template at `services/audio/src/bush_stt/postprocess.py`.

Auto-decisions:
- **NEW TODO P1:** Audit transcript logging — if PII persisted, redact or expire.
- **NEW TODO P1:** Document the Ollama deployment topology (local vs remote) in operator runbook.
- All three are minor relative to ship-blockers; defer to P1.

#### Section 4: Data Flow & Interaction Edge Cases

For each TODOS item, the shadow paths:

**Cold-start readiness:**
```
  STARTUP ──> MQTT.connect ──> _load_model (3-8s) ──> publish ready=true
    │             │                    │                      │
    ▼             ▼                    ▼                      ▼
  [systemd     [broker          [model load       [retained msg
   restart]    timeout?]        fails?]            never seen by
                ↓                    ↓              early subscriber?]
              [services         [systemd                   ↓
               block at         restart loop          [subscriber
               startup ──         ↓                    needs to retry
               eng #5             [infinite                or use QoS]
               WSL2]               warmup —
                                  partial detect via
                                  systemd RuntimeMaxSec]
```

Edge case: subscriber connected to broker before publisher published `ready=true`. Broker delivers retained message on subscribe, so this is OK as long as the message is published with retain=True. **Verify the retained flag is set** when adding `ready` publish — easy to miss.

**Pico reconnect during pulse:**
```
  flare_pulse(220ms) ──> service_pins (clock OFF deadline)
                              │
                              ▼
                          [mqtt_open in ST_RETRY_CONFIGURED]
                              │
                              ▼ 5s blocking
                          [valve stays open up to 5s ──
                           PROPANE SAFETY VIOLATION]
```

Mitigation per eng review: force-OFF all relays before entering blocking states. Logging requirement: publish a `bush/fire/safety/forced_off` event so operators see when this fires.

#### Section 5: Code Quality

N/A for a punch list. Code quality findings against actual implementations belong in eng review (Phase 3).

#### Section 6: Test Review

Test diagram for the expanded backlog:

```
NEW UX FLOWS:
  - Cold-start happy: visitor speaks immediately after `systemctl restart bush-stt`
  - Cold-start partial: visitor speaks during 3-8s warmup window (currently broken: dead air)
  - MQTT broker blip: broker drops 60s mid-burn, recovers (currently broken: sentiment dies silently)
  - Pico reconnect: Wi-Fi flap 5s with active flare pulse (currently broken: stuck solenoid)

NEW DATA FLOWS:
  - bush/<service>/ready retained-msg lifecycle
  - bush/<service>/fault topic publication on internal error

NEW CODEPATHS (per item):
  - sentiment: post-model-load `classifier()` shape verification (eng §9.A)
  - sentiment: MQTT loop reconnect / fault publish on `loop_forever()` exit
  - bushutil: `subprocess.run` with timeout=5 in `get_mqtt_broker()`
  - firmware: non-blocking `s.connect_ex()` in mqtt_open + force-OFF before SCAN_PROBE
  - all services: ready publish in on_connect after model load
  - t2v: query timeout fallback to `STOCK_FALLBACK_VERSE` (eng §2.9 suggestion)

NEW INTEGRATIONS:
  - integration test waits on `bush/<service>/ready=true` before audio inject
  - integration test verifies fault topic on simulated failure
  - bush-pray sox upsamples 16k → 48k for STT_USE_RNNOISE=1 path

NEW ERROR/RESCUE PATHS:
  - sentiment classifier output mismatch → fault topic publish
  - sentiment thread death → systemd-visible exit (not daemon thread)
  - Pico mqtt_open timeout during pulse → force-OFF event published
```

Test gaps:
- **CRITICAL:** No regression test for the legacy path (`STT_USE_VAD=0 STT_ENGINE=vosk TTS_ENGINE=espeak`). The eng review's "is byte-identical to today's behavior" promise is unverified.
- **CRITICAL:** No test for `STT_USE_RNNOISE=1` path with bush-pray fixture (ship blocker).
- **HIGH:** No bench-on-M2 results for current memory budget under simultaneous (whisper-bindings + DistilBERT + Piper + RNNoise + ChromaDB).
- **HIGH:** No thermal soak test — does the system degrade after 4 hours of continuous operation in 100°F+?

Friday-2am test: would the system survive `kill -9 mosquitto` at midnight? Currently no — sentiment thread dies. Post-fix: systemd should see services exit, `Restart=always` should bring them back, fault topics should publish.

Hostile-QA test: malicious-speech visitor saying "ignore your previous instructions" into the bush. STT_LLM_CORRECT=1 path could be prompt-injected. Verify the postprocess.py prompt template guards this.

#### Section 7: Performance Review

The performance findings are themselves the items in TODOS (NPU Tiers). Per dual voices: don't accelerate before observability. Defer. No new findings.

#### Section 8: Observability & Debuggability — CRITICAL

This is where both dual voices converged hardest. Findings:

1. **No `bush/<service>/fault` topic.** Every failure mode currently signals via silence. Operator at 3am phone-flashlight cannot tell sentiment-broken from t2v-broken from STT-broken.
   - **Fix:** add fault topic to the MQTT contract upgrade (with `ready` and `version`). One coherent change.
2. **No `bush/<service>/version` field.** When you SSH to the box at playa and find a misbehaving service, you cannot tell which commit it's running. Field upgrades need this.
   - **Fix:** publish git short-hash or version string in the `ready` message payload.
3. **No structured logging.** Currently `print()` calls. journalctl works but log queries (grep, jq) don't. Volunteer at 3am gets walls of text.
   - **Fix:** P2 — switch to structured (one JSON line per event) over the playa cycle. Pre-playa: keep prints, add per-service `bush <service> log` helper that tails+greps.
4. **No alerts.** Discord bot already exists for `/pray`. Subscribe to `bush/<service>/fault` and post to a private channel. Effort: ~1 hour.
5. **No operator runbook.** Cannot be replaced by alerts; volunteer needs a flowchart.
   - **Fix:** 1-page PDF, hangs in the trailer. Each fault topic → action.
6. **bush-monitor doesn't subscribe to valve topics** (eng #13). Already a known doc-drift gap.
7. **Dashboards:** Grafana is overkill. Two terminal panes via `tmux send-keys` to journalctl filtering on a per-service basis is enough.

Auto-decisions:
- **NEW TODO P0:** MQTT contract upgrade (`ready` + `fault` + `version`) as a single coherent item, not three.
- **NEW TODO P0:** Operator runbook (1-page PDF).
- **NEW TODO P1:** Discord alert on fault topics.
- **NEW TODO P1:** bush-monitor valve subscription fix.
- **NEW TODO P2:** Structured logging migration.

#### Section 9: Deployment & Rollout

Field-deploy considerations:

- **Truck-roll deadline.** Approx late August. Today is 2026-05-07 → ~3.5 months. Realistic engineering capacity: 4 productive days/week × ~14 weeks × ~6h/day = ~336 hours human-equivalent. With CC compression: about 30-50 hours of "real" engineering decisions, plus verification deploys (each ~5 min wall-clock to ODROID, plus integration test ~3 min).
- **No staging environment.** Bench test = WSL2 dev. Integration test runs on dev box. ODROID = production. Risk: WSL2-specific behavior (eng #5 broker discovery) doesn't surface until production.
- **Rollback plan.** Each shipping change is gated on a flag (STT_ENGINE, TTS_ENGINE, STT_USE_VAD, STT_USE_RNNOISE, STT_LLM_CORRECT). Rollback = `systemctl edit` + `systemctl restart`. Fast.
- **Pre-playa freeze.** No work-in-flight in the last 7 days before truck-roll. Verification cycle only. **Add this as an explicit P0 calendar item.**
- **Boring-default fallback configuration.** Per Codex finding: name the proven config that worked at 2025 burns or in deep bench. Default flags to that for first-trip-around. Risky-flag config is a `bushctl risky-default` toggle the operator runs after the bush has been alive 30 min.

**NEW TODO P0:** Pre-playa freeze date + boring-default fallback configuration.

Auto-decisions logged.

#### Section 10: Long-Term Trajectory

Reversibility check (1 = one-way door, 5 = easily reversible):
- All current TODOS items: **5/5** (feature-flagged or local fixes, no DB migrations, no API contracts external to the device)
- Voice cloning would be **3/5** — once a specific voice is "the bush," changing it later is a brand decision, not a tech decision

Path dependency:
- Adding fault/ready/version MQTT topics now is path-dependent in the right direction — every future service inherits the contract.
- Deferring NPU Tiers 2/3 to post-playa is fine; the engine adapter already enables drop-in offload.
- Voice cloning post-playa is fine.

Knowledge concentration:
- All recent commits author = wuff. Bus factor = 1. No mitigation in TODOS.
- Volunteer-friendly operator runbook (per Section 8) is the only meaningful path to broaden bus factor at field-deploy time.

The 1-year question: an engineer reading the post-playa repo in 2027 should be able to follow STT_TTS-EFFICACY.md, STT_TTS-ENG-REVIEW.md, and a (future) post-playa retro to understand "what shipped, what worked, what didn't." If TODOS.md is reconciled and tracks closure on every item, this works. Currently with 8 silent drops, it doesn't.

#### Section 11: Design — SKIPPED (no UI scope detected)

### CEO Required Outputs

#### NOT in scope (deferred with rationale)

| Item | Why deferred |
|---|---|
| NPU Tier 1 — Whisper on RKNN | Calendar risk: ONNX→RKNN conversion is the most uncertain pre-playa engineering. Captured as P2 stretch; lift if verification cycles slip *into* the calendar (not out of it). |
| NPU Tier 2 — DistilBERT on RKNN | Free side win once observability lands. P2 stretch post-playa. |
| NPU Tier 3 — Qwen3 embedding via OpenVINO | Implementation already exists (middog/t2v 76cdf96b). Deferred to v1.5 post-playa per existing TODOS rationale. Confirmed unchanged. |
| Voice cloning XTTS-v2 | Subagent finding 6: art-direction question, not engineering. Move to `IDEAS.md` or `/office-hours` brainstorm. Out of TODOS. |
| Structured logging migration | Stretch P2; print() to journalctl is enough for field deploy. |
| Grafana / Prometheus stack | Overkill for a single box. Two `tmux` panes + journalctl is enough. |
| Voice rights / IP / persona alignment | Design conversation, not engineering. |

#### What already exists (and the plan reuses)

| Sub-problem | Existing code |
|---|---|
| Lazy model load post-MQTT-connect | `services/sentiment/src/bush_sentiment/__init__.py:12` (`classifier = None; _load_model()` after MQTT setup). Pattern can be replicated to STT and TTS. |
| Engine-adapter contract for hot-swappable models | `services/audio/src/bush_stt/engines/base.py` (Vosk, whisper-bindings, whisper-subprocess). Future RKNN engine slots in here. Same pattern in `bush_tts` (Piper, espeak). |
| Flag-gated rollout/rollback | `STT_ENGINE`, `TTS_ENGINE`, `STT_USE_VAD`, `STT_USE_RNNOISE`, `STT_LLM_CORRECT`, `STT_MIN_CONFIDENCE`, `T2V_DEVICE`, `SENTIMENT_NPU` already documented. Rollback = `systemctl edit` + restart. |
| NPU readiness check | `utils/bush-npu-check` (5-stage RKNN gate, exit codes 0/1/2). Future RKNN work gates on this. |
| Integration test framework | `utils/bush-integration-test` has the `Stage` class for waiting on MQTT topics. Adding new stages is additive. |
| Tier-0/1/2/3 sequencing | `docs/STT_TTS-ENG-REVIEW.md:393-431` already triages the eng review findings into ordered tiers. Reuse that ordering verbatim. |
| MQTT broker discovery | `bushutil.get_mqtt_broker()` works; needs `subprocess` timeout (eng #5). |
| Discord bridge | Already exists (`bush-discord`). Subscribe to fault topics for free alerts. |
| LLM post-correct + USB codec udev + confidence gate | Salvage commit `5ec304f` already shipped. |
| t2v archive at middog/t2v 76cdf96b | OpenVINO embedder branch preserved, ready for format-patch when v1.5 cycle starts. |

#### Dream state delta

Post-reconciled TODOS executes A1+A3 (P0+P1 ship-blockers). After execution, system delta vs 12-month ideal:

```
  POST-RECONCILED EXECUTION (Aug 2026)         12-MONTH IDEAL (May 2027)
  ───────────────────────────────────         ─────────────────────────
  - Cold-start gap closed                      - All NPU tiers (1, 2, 3) shipped
  - Fault topics + ready + version             - Voice cloning landed (post-design)
  - Pico safety fix                            - Operator runbook is field-tested
  - Operator runbook + boring-default          - Bus factor > 1 (volunteer training)
  - Bench harnesses run on M2 in soak          - Post-playa retro shapes v2 architecture
  - Integration test covers all flags          - Multi-device installation patterns
                                                surface (multiple bushes)

  NEAR-MISS:
  - NPU Tier 1 (Whisper-RKNN) likely deferred — accept calendar risk vs reward
  - Field validation = 1 dry-run weekend, not deep
  - Bus factor still 1 at the burn (acceptable for art install with skilled author on-site)
```

The plan moves us most of the way there for the August picture and is correctly phased for the 12-month picture. The risk is the calendar — 3.5 months is tight given verification cycles + parallel motorized-needle-valve work.

#### Error & Rescue Registry

(See Section 2 table above. 4 critical gaps logged as P0 ship blockers per premise gate.)

#### Failure Modes Registry

| Codepath | Failure mode | Rescued? | Test? | User sees | Logged? |
|---|---|:-:|:-:|---|:-:|
| Cold-start (any service) | Model loads slowly; subscriber sees no data | N | N | bush silent for 3-8s | partial |
| sentiment thread | MQTT loop_forever exit | **N** | N | bush emotionally flat for hours | **N CRITICAL** |
| sentiment classifier | API drift (eng §9.A) | **N** | N | bush emotionally flat for hours | **N CRITICAL** |
| bushutil get_mqtt_broker WSL2 | Subprocess hang at startup | **N** | N | all services hang on dev box | **N** |
| Pico mqtt_open during pulse | 5s solenoid stuck open | **N** | N | propane stays open up to 5s | **N CRITICAL** |
| t2v query | Ollama / Chroma timeout | **N** | N | bush silent | **N CRITICAL** |
| RNNoise + bush-pray | 16k vs 48k mismatch | N | N | integration test broken | partial |
| Whisper "confidence" | Sentinel 1.0 always | N | N | confidence gate is a no-op | partial |
| Piper subprocess | Binary missing | unknown | N | bush silent | partial |

**5 critical gaps** flagged. All reconciled into the expanded TODOS as P0 (cold-start, sentiment thread, sentiment classifier, Pico, t2v fallback) per Premise Gate decision A.

#### Diagrams

Architecture diagram for the MQTT contract upgrade (the highest-impact single change):

```
  ┌─ AT STARTUP (broker connect) ────────────────────────────────────┐
  │                                                                  │
  │   service:                                                       │
  │     1. mqtt.connect()                                            │
  │     2. mqtt.subscribe("bush/<service>/cmd")                      │
  │     3. mqtt.start_loop()                                         │
  │     4. publish bush/<service>/version (retain=true)              │
  │     5. _load_model()  ←── 3-8s for whisper, ~20s for distilbert  │
  │     6. publish bush/<service>/ready true (retain=true)           │
  │                                                                  │
  └──────────────────────────────────────────────────────────────────┘

  ┌─ AT RUNTIME (any service exception or thread death) ─────────────┐
  │                                                                  │
  │   on_mqtt_disconnect() callback:                                 │
  │     publish bush/<service>/ready false (retain=true)             │
  │   on application exception:                                      │
  │     publish bush/<service>/fault {error,ts,context} (no retain)  │
  │     re-raise so systemd sees process exit                        │
  │                                                                  │
  └──────────────────────────────────────────────────────────────────┘

  ┌─ INTEGRATION TEST ───────────────────────────────────────────────┐
  │                                                                  │
  │   for unit in [stt, t2v, tts, sentiment, flame-expression]:     │
  │     wait_for_retained("bush/" + unit + "/ready", "true", 30s)   │
  │     if timeout:                                                  │
  │       FAIL(f"{unit} never became ready — model load failed?")   │
  │   inject_audio()                                                 │
  │   wait_for(transcript, verse, speaking, done, sentiment)        │
  │                                                                  │
  └──────────────────────────────────────────────────────────────────┘
```

Pico safety fix data flow (eng review #3 simplified version):

```
  ENTER ST_RETRY_CONFIGURED (or ST_SCAN_CONNECT):
    │
    ▼
  force_off_all_relays()  ←── new
  publish bush/fire/safety/forced_off {state, ts} ←── new
    │
    ▼
  s.connect() with timeout (still 5s, blocking — accepted tradeoff)
    │
    ▼
  Connected? → return to STATE_CONNECTED
  Failed?    → retry next loop (relays remain forced_off)
```

**Stale Diagrams Audit:** `docs/mqtt-architecture.dot` does not have `/ready`, `/fault`, or `/version` topics. **Update in same commit as the contract upgrade.** (Per CEO Prime Directive #6: stale diagrams are worse than none.)

#### Decision Audit Trail

| # | Phase | Decision | Classification | Principle | Rationale |
|---|---|---|---|---|---|
| 1 | CEO 0A | Premises rotted; TODOS missing 8 of 14 eng review findings | Mechanical | P1 completeness | Documented in tabular evidence; both voices confirm |
| 2 | CEO 0C-bis | Approach A (reconciliation + sequenced) over B (express) and C (Tier 1 NPU pre-playa) | Taste | P3 pragmatic + P2 boil lakes | C accepts higher calendar risk; A still in blast radius and <1d to add the items |
| 3 | CEO 0D | Cherry-pick A, B, C, D, E, F, G accepted into TODOS | Mechanical | P2 boil lakes | All in blast radius; some belong in pre-playa, some post |
| 4 | CEO 0F | Mode SELECTIVE EXPANSION confirmed | Mechanical | autoplan rule | Default for feature enhancement / iteration |
| 5 | CEO §1 | MQTT contract upgrade is one item, not three | Mechanical | P3 pragmatic | Eng review §10 explicitly groups them; touching code twice is waste |
| 6 | CEO §1 | Sequence: firmware → MQTT contract → service-internal → NPU | Mechanical | P5 explicit | Pico is slowest reset path |
| 7 | CEO §2 | 4 critical gaps from error map → P0 ship blockers | Mechanical | premise gate decision A | User confirmed reconciliation |
| 8 | CEO §3 | Audit transcript logging + Ollama topology in runbook → P1 | Taste | P1 completeness | Adds 2 small items; out of scope to scope-reduce |
| 9 | CEO §6 | Add P0 test for legacy regression + RNNoise + bush-pray | Mechanical | P1 completeness | Eng review's "byte-identical" promise is unverified |
| 10 | CEO §6 | Friday-2am test + hostile-QA test added as test categories | Mechanical | P1 completeness | Standard for pre-playa harness |
| 11 | CEO §8 | Discord alerts on fault topics → P1; structured logging → P2 | Mechanical | P3 pragmatic | Discord exists; logging migration is post-playa polish |
| 12 | CEO §9 | Pre-playa freeze date + boring-default fallback config → P0 | Mechanical | P5 explicit | One-shot deploy; no risky-flag-day-1 |
| 13 | CEO §9 | Voice cloning out of TODOS, into IDEAS.md | Taste | P3 pragmatic + subagent finding 6 | Art-direction belongs in design surface |

#### Completion Summary

```
+========================================================================+
|                  CEO REVIEW — COMPLETION SUMMARY                       |
+========================================================================+
| Mode selected         | SELECTIVE EXPANSION (autoplan rule)            |
| System Audit          | 5 docs read, 14-finding eng review surfaced   |
| Step 0                | 3 premise rots flagged; approach A picked     |
| Section 1  (Arch)     | 0 new issues (sequencing logged)              |
| Section 2  (Errors)   | 8 codepaths mapped, 4 CRITICAL GAPS           |
| Section 3  (Security) | 2 P1 items, 0 P0                              |
| Section 4  (Data/UX)  | shadow paths mapped, 1 retain-flag gotcha     |
| Section 5  (Quality)  | N/A (punch list)                              |
| Section 6  (Tests)    | diagram produced, 4 gaps (2 CRITICAL)         |
| Section 7  (Perf)     | findings ARE the items (no new)               |
| Section 8  (Observ)   | 5 P0+P1 items, biggest blind spot zone        |
| Section 9  (Deploy)   | 2 new items: freeze date + boring fallback    |
| Section 10 (Future)   | Reversibility 5/5; bus factor 1 mitigation    |
| Section 11 (Design)   | SKIPPED (no UI scope)                         |
+------------------------------------------------------------------------+
| NOT in scope          | written (7 items)                              |
| What already exists   | written (10 items)                             |
| Dream state delta     | written                                        |
| Error/rescue registry | 8 methods, 4 CRITICAL GAPS                    |
| Failure modes         | 9 total, 5 CRITICAL GAPS                      |
| TODOS.md updates      | 14 items proposed (P0/P1/P2 reconciled)       |
| Scope proposals       | 7 surfaced, 7 accepted (by tier)              |
| CEO plan              | written (this section)                        |
| Outside voice         | ran (codex + Claude subagent)                 |
| Lake Score            | 12/13 recommendations chose complete option   |
| Diagrams produced     | 3 (MQTT contract, Pico safety, integration)   |
| Stale diagrams found  | 1 (docs/mqtt-architecture.dot)                |
| Unresolved decisions  | 1 (premise gate result requires user re-confirm) |
+========================================================================+
```

#### Unresolved decisions

The premise gate has been answered (option A: expand TODOS). The actual reconciled-and-restructured TODOS will be presented at the Phase 4 final approval gate so the user sees the new shape before it lands. No CEO-phase questions remain.

## Phase 3: Eng Review

### Step 0: Scope Challenge (with code reads)

The eng review's scope challenge against TODOS as-written + the premise-gate-confirmed expansion. This phase tests whether the proposed *implementation patterns* hold up under code-level scrutiny — not just whether the items are in the list.

Files actually read (for grounding):
- `services/sentiment/src/bush_sentiment/__init__.py:5,12,184-193` — torch threads=1, lazy classifier load, daemon thread MQTT loop
- `services/audio/src/bush_stt/__init__.py:54,303-318,385` — RNNoise 48k flip, status topic patterns, `_run_legacy_iteration`
- `services/audio/src/bush_stt/postprocess.py:27,40,55` — LLM post-correct prompt template (raw f-string interpolation)
- `services/audio/tests/test_main_loop.py:1` — existing STT tests explicitly exclude capture/MQTT loop
- `services/core/src/bush_t2v/__init__.py:127` — t2v MQTT client subscribe ordering
- `services/core/src/bush_tts/__init__.py:355` — tts MQTT subscribe ordering
- `utils/bush-integration-test:112,160,209,222` — `connected.set()` SUBACK race, `Stage` class, transcript inject ordering
- `utils/bush-pray:39,40-50,110-114` — espeak 16k synth, sox pipe, blind PulseAudio playback
- `firmware/relay-control/CIRCUITPY/code.py:213-264,232,248,466-538,491-522` — `tcp_probe` 0.5s, `mqtt_open` 5s, ST_RETRY_CONFIGURED + ST_SCAN_PROBE state machine
- `packages/bushutil/` — broker discovery, no subprocess timeout
- `systemd/odroid/*.service` — only `After=network.target`, no `EnvironmentFile=`, no `Restart=always` audit

Complexity check: post-expansion the punch list has ~14-18 items. Some are minutes of work; some are days. The complexity is in the **MQTT contract upgrade** (1 item but touches 5 services + integration test + diagrams). This is the legitimate scope concentration; it is not over-engineering. Boring-default fallback config, operator runbook, and pre-playa freeze are calendar items, not code items, and don't add complexity.

Search check: paho-mqtt's reconnect model (`connect_async`, `reconnect_delay_set`, `loop_start`, `on_connect`, `on_disconnect`) is tried-and-true Layer 1. Both eng voices independently arrive at this pattern. CircuitPython's MQTT in `code.py` is a custom hand-rolled client; non-blocking `s.connect_ex()` + state-machine poll is well-trodden Layer 1 for embedded TCP.

### Step 0.5: Dual Voices

#### CODEX SAYS (eng — architecture challenge)

1. **Critical (9/10):** `bush/<service>/ready` retained contract is underspecified. Current services do not set LWT/birth state. Several MQTT clients subscribe outside `on_connect` or exit on initial broker failure (`services/sentiment/__init__.py:140`, `services/core/bush_t2v/__init__.py:127`, `services/core/bush_tts/__init__.py:355`, `utils/bush-integration-test:112`). A retained `ready=true` without retained `ready=false` on disconnect becomes a stale lie. **Fix:** define contract as `status=offline|starting|ready` on one retained topic, set MQTT `will_set(...offline...)`, publish `starting` after connect, publish `ready` after warmup, republish from `on_connect` whenever `ready_latched` is true.
2. **High (8/10):** Integration harness races its own subscriptions. `connected.set()` fires after `subscribe()` not after SUBACK (`utils/bush-integration-test:160,209,222`). Test may publish transcript before broker-side subscriptions are active. **Fix:** wait for `on_subscribe` for all topic mids before injecting, or use a probe/ack topic.
3. **Critical (10/10):** "Force OFF before blocking states" understates where blocking happens. `mqtt_open()` blocks up to 5s during CONNECT/CONNACK at `code.py:232`, and is called from reconnect/scan states at `code.py:465,525`. **Fix:** add `force_all_solenoids_off()` that sets pins low + clears all `off_ms_*` deadlines. Call it before EVERY path that can block (`wifi_connect`, `mqtt_open`, `tcp_probe`, scan verify).
4. **High (9/10):** Sentiment self-heal needs a concrete paho restart model. `connect()` + `loop_forever()` in daemon thread, catches one exception, dies silently (`services/sentiment/__init__.py:184`). Also: subscribing before model load drops inbound publishes while `classifier is None` (`:160`). **Fix:** load model first, MQTT in main thread; OR `connect_async()` + `reconnect_delay_set()` + `loop_start()` + `on_connect`/`on_disconnect`; no wrapper thread that vanishes.
5. **Medium (9/10):** "Byte-identical regression test" cannot be proved with current end-to-end harness. STT unit tests exclude capture/MQTT loop (`services/audio/tests/test_main_loop.py:1`); byte-identical contract lives inside `_run_legacy_iteration()` (`services/audio/src/bush_stt/__init__.py:385`). **Fix:** extract legacy publish logic behind fake recognizer/fake MQTT seam; feed fixed PCM + result sequences; assert exact topic order + payload bytes.
6. **Medium (8/10):** `bush-pray` fix needs to cover cached files and avoid bifurcating test semantics. Synthesis writes 16k WAVs (`utils/bush-pray:39,110`); RNNoise mode flips capture to 48k (`services/audio/src/bush_stt/__init__.py:54`). Upsample-on-synthesis leaves cached 16k diverging; bypass-RNNoise stops testing real input. **Fix:** keep one black-box harness; upsample in playback pipe based on active STT mode; force mono 48k s16le for both generated + cached.
7. **High (8/10):** Punch list missing prompt-injection hardening (`postprocess.py:27,55` interpolates raw transcript) AND stale-retained cleanup as first-class items. **Fix:** delimiter/JSON-wrapped correction input + hostile transcript tests; `ready` topic LWT/offline semantics + broker-blip recovery assertions.

**Codex ENG BLIND SPOT:** *"The backlog treats readiness as a green-light bit, but the real architectural gap is lifecycle truthfulness under reconnects, crashes, and stale retained state."*

#### CLAUDE SUBAGENT (eng — independent review)

F1 (high, 9/10) — `ready` retained-msg has subscribe-vs-publish race; verify mosquitto persistence; need `will_set(...false)` LWT.
F2 (high, 10/10) — sentiment daemon-thread silent-death; same pattern audit needed for sound/audio-agent/t2v/tts.
F3 (critical, 10/10) — Pico force-OFF missing entirely from punch list; eng review §2.3 calls it the most playa-relevant safety finding.
F4 (high, 9/10) — "byte-identical" claim unfalsifiable; need pinned model SHA + fixed WAV + JSON byte-for-byte fixture.
F5 (medium, 8/10) — bush-pray 16k → 48k upsample is not equivalent; "bypass" alternative is right answer; espeak signature padded to 8kHz Nyquist confuses RNNoise.
F6 (medium, 9/10) — LLM post-correct hostile speech: visitor whispers slurs, bush speaks them; needs whitelist clamp + adversarial corpus + TTS-side profanity gate.
F7 (medium, 8/10) — Discord audio-recv: PII / consent / WAVS_DIR rotation / signage; verify `EnvironmentFile=` mode 0600.
F8 (medium, 8/10) — DistilBERT NPU port: int8 quant can shift argmax label; need `match rate ≥ 0.97 on N=200 verses` gate before flipping `SENTIMENT_NPU=1`.
F9 (high, 9/10) — No load test, no thermal soak, no brownout simulation. `torch.set_num_threads(1)` was M1 mitigation; removing without proving M2 doesn't brown out is a bet.
F10 (medium, 7/10) — No rollback / feature-flag matrix in repo. Need `/etc/systemd/system/bush-*.service.d/playa.conf` overrides + `bush-rollback` script.

**Subagent ENG BLIND SPOT:** *"The plan treats `ready` retained-messages as the cold-start cure when the actual playa-killer is silent unsupervised drift — Pico 5s pin lock, sentiment thread death, t2v silent timeout — none of which a `ready=true` boolean published once at startup will catch."*

#### ENG DUAL VOICES — CONSENSUS TABLE

```
═══════════════════════════════════════════════════════════════════════════════
  Dimension                              Claude    Codex     Consensus
  ────────────────────────────────────── ─────── ────────── ──────────────────
  1. Architecture sound?                 NO       NO         CONFIRMED needs upgrade
  2. Test coverage sufficient?           NO       NO         CONFIRMED no
  3. Performance risks addressed?        NO       partial    CONFIRMED no (load+thermal+brownout missing)
  4. Security threats covered?           NO       NO         CONFIRMED no (prompt-inj + PII)
  5. Error paths handled?                NO       NO         CONFIRMED no (silent-fail recovery model)
  6. Deployment risk manageable?         NO       NO         CONFIRMED no (no rollback automation)
═══════════════════════════════════════════════════════════════════════════════
```

**6/6 confirmed agreement** that the plan is in the right direction but the implementation contracts need tightening. Both blind-spot calls converge on **lifecycle truthfulness** as the unmet need. Strong signal.

### Section 1: Architecture (with ASCII diagram)

The biggest single architectural change is the MQTT contract upgrade. Both voices agreed on the corrected shape:

```
  ┌── MQTT CONTRACT UPGRADE (per service) ─────────────────────────────────────┐
  │                                                                            │
  │   PRE-CONNECT:                                                             │
  │     client.will_set("bush/<svc>/status", "offline", retain=True, qos=1)   │
  │                                                                            │
  │   POST-CONNECT (in on_connect):                                            │
  │     publish("bush/<svc>/status", "starting", retain=True)                 │
  │     publish("bush/<svc>/version", git_short_hash, retain=True)            │
  │     subscribe(<topics>)  ←── BEFORE returning from on_connect              │
  │     # (ready_latched preserves status across reconnects)                   │
  │     if ready_latched: publish("bush/<svc>/status", "ready", retain=True)  │
  │                                                                            │
  │   AFTER MODEL/WARMUP COMPLETE:                                             │
  │     ready_latched = True                                                   │
  │     publish("bush/<svc>/status", "ready", retain=True)                    │
  │                                                                            │
  │   ON ERROR / EXIT:                                                         │
  │     publish("bush/<svc>/fault", {error,ts,context}, retain=False)         │
  │     # status flips to "offline" via LWT (if disconnect) or                 │
  │     # via explicit publish before exit (if controlled shutdown)            │
  │                                                                            │
  └────────────────────────────────────────────────────────────────────────────┘

  Subscriber view (e.g., bush-discord alerting):
    bush/<svc>/status retained = offline | starting | ready
    → if ready: bush is alive
    → if offline (LWT-driven): bush died
    → if starting (>30s): bush is hung in warmup
    bush/<svc>/fault → ad-hoc exception payload
```

Architectural decisions logged:
- One retained `status` topic with `offline|starting|ready` enum is correct over two topics (`/ready` boolean + `/status` enum); two topics doubles the contract surface.
- LWT is essential — without it, retained-true outlives crashes and lies for hours.
- `version` is a one-shot retained publish at startup. Cheap; high-leverage when SSH-debugging at playa.

ASCII dependency graph (post-reconciliation):

```
                              ┌─────────────────────┐
                              │ Pico force-OFF (P0) │  ←── firmware-only, no deps
                              └──────────┬──────────┘
                                         │
                    ┌──────────────┬─────┴─────┬──────────────┐
                    ▼              ▼           ▼              ▼
            ┌───────────┐  ┌──────────┐  ┌──────────┐  ┌──────────┐
            │ MQTT      │  │ bushutil │  │ bush-pray│  │ Boring   │
            │ contract  │  │ broker   │  │ loopback │  │ default  │
            │ upgrade   │  │ timeout  │  │ fix      │  │ config   │
            │ (5 svcs)  │  │ (eng #5) │  │          │  │          │
            └─────┬─────┘  └──────────┘  └──────────┘  └──────────┘
                  │
        ┌─────────┴──────────┐
        ▼                    ▼
  ┌──────────┐        ┌─────────────┐
  │ Sentiment│        │ Integration │
  │ self-heal│        │ test extn   │
  │ (eng #2) │        │ + SUBACK    │
  │ + §9.A   │        │   race fix  │
  └──────────┘        │ + byte-id   │
                      │   regression│
                      │ + LLM eval  │
                      └─────────────┘

  Operator runbook → consumes status/fault topics → ships with all of above
```

### Section 2: Code Quality

Specific issues with file:line citations:

| Issue | Where | Fix shape |
|---|---|---|
| `services/sentiment/__init__.py:5` `torch.set_num_threads(1)` is M1-RK3568 brownout mitigation; comment doesn't say "remove on M2" | sentiment | Document as a feature flag: `BUSH_TORCH_SINGLE_THREAD=1` (default unset on M2). Verify M2 doesn't brown out under sustained load before flipping. |
| `services/sentiment/__init__.py:184-193` daemon-thread MQTT loop with one try/except | sentiment | Per Codex eng#4 fix: load model first; MQTT in main thread; or `connect_async + reconnect_delay_set + loop_start + on_disconnect`. Pattern audit needed for sound, audio-agent, t2v, tts. |
| `services/audio/src/bush_stt/postprocess.py:27,40,55` raw transcript interpolated into prompt | LLM post-correct | Wrap user transcript in delimiters + JSON; add post-LLM length+regex whitelist; add hostile-corpus eval. |
| `utils/bush-integration-test:160,209,222` `connected.set()` fires before SUBACK | integration test | Wait for `on_subscribe` for all topic mids before injecting. |
| `firmware/relay-control/CIRCUITPY/code.py:232,248` `s.settimeout(5)` blocks main loop during reconnect | firmware | Force-OFF before every blocking path. Eventually: non-blocking state-machine `s.connect_ex` + poll. |
| `packages/bushutil/.../*.py` `subprocess(['ip', 'route'])` no timeout | bushutil | `subprocess.run(..., timeout=2)` with `try/except TimeoutExpired: return "localhost"` fallback. |
| `services/core/src/bush_t2v/__init__.py:127` MQTT subscribe ordering | t2v | Subscribe inside `on_connect`, not in main thread. (paho convention.) |
| `services/core/src/bush_tts/__init__.py:355` MQTT subscribe ordering | tts | Same as above. |

DRY: every service is going to grow the same MQTT-contract-upgrade boilerplate. **Extract `bushutil.mqtt.MqttServiceClient`** with: `will_set`, `on_connect`, `publish_status`, `publish_fault`, `publish_version`, `set_ready_latched`. ~80 lines. All 5 services collapse to 3-line usage. Adds a TODO item but is the right shape — Codex eng#7 implicitly endorses by calling out "stale-retained cleanup as a first-class backlog item."

### Section 3: Test Review (with test plan artifact)

Test diagram for the expanded backlog:

```
CODE PATHS                                             USER FLOWS
[+] services/audio/src/bush_stt/__init__.py            [+] Cold-start
  ├── _run_legacy_iteration() (line 385)                 ├── [GAP★★★ regression] STT_USE_VAD=0 STT_ENGINE=vosk
  │   └── [GAP] byte-identical fake-MQTT seam test        │     byte-identical to commit a2094e4 baseline
  └── _run_pipeline_iteration()                          ├── [GAP★★★] Visitor speaks during 3-8s warmup
      ├── [TESTED ★] VAD endpointer (lane 4 unit tests)   │     status=starting → status=ready
      ├── [GAP★★★] STT_USE_RNNOISE=1 + bush-pray fixture │     subscriber sees status delta, not silence
      ├── [GAP] STT_LLM_CORRECT=1 hostile-speech corpus   ├── [GAP★★★] Operator restart during pulse
      └── [GAP] confidence gate + whisper sentinel       │     Pico force-OFF visible on operator dashboard
                                                         │
[+] services/sentiment/src/bush_sentiment/__init__.py    [+] Broker blip mid-burn
  ├── classifier(...) — return_all_scores=True          │     ├── [GAP★★★] mosquitto kill -9 + restart
  │   └── [GAP★★★] verify shape against current        │     │     sentiment self-heals; bush-monitor sees
  │         transformers (eng §9.A)                      │     │     status=offline → starting → ready
  ├── on_message handler (line 153-179)                 │     └── [GAP] retained `ready=true` re-broadcast
  │   └── [GAP★] try/except path (test that errors       │           on broker restart
  │         publish fault topic, not silent)             │
  └── MQTT loop_forever (line 184-193)                   [+] Hostile speech
      └── [GAP★★★] simulated loop exit                    ├── [GAP] "ignore previous instructions ..."
            → status=offline LWT fires                    │     STT_LLM_CORRECT=1 path returns clamped output
            → systemd restarts service                    └── [GAP] Profanity / slur visitor input
                                                                 TTS-side gate prevents bush from speaking it
[+] firmware/relay-control/CIRCUITPY/code.py
  ├── mqtt_open (line 232)                              [+] Thermal soak
  │   └── [GAP★★★] integration test: kill broker         ├── [GAP] 1hr continuous load + heat-gun enclosure
  │         during simulated flare; assert pin OFF       │     RTF stable, no thermal throttle
  │         deadline met (within 50ms tolerance)         └── [GAP] Brownout: USB codec recovery, sentiment
  └── ST_SCAN_PROBE / ST_RETRY_CONFIGURED                       OOM behavior

[+] utils/bush-pray
  └── playback pipe (line 110)                          [+] Discord PII
      └── [GAP] sox upsample 16k→48k driven by             ├── [GAP] WAVS_DIR rotation policy
            STT_USE_RNNOISE env var, applies to             └── [GAP] consent signage placement spec
            cached fixtures too (Codex#6)

LLM eval: [GAP→EVAL] postprocess.py prompt change → 50-prompt corpus (10 hostile, 20 neutral, 20 in-domain)
                       baseline = pre-clamp output; new = post-clamp output

COVERAGE: 4/22 paths tested (18%)  |  CRITICAL gaps: 11  |  E2E gaps: 7  |  Eval gaps: 1
```

**REGRESSION RULE flag:** The legacy path (STT_USE_VAD=0 STT_ENGINE=vosk TTS_ENGINE=espeak) was rewritten in commits 90df8cc + a9526e4. The promise is byte-identical behavior with flags off. **No test pins this.** Codex eng#5 is right: extract legacy publish logic behind a fake-recognizer/fake-MQTT seam; assert exact topic order + payload bytes. **CRITICAL.** This regression test is mandatory; no AskUserQuestion needed.

### Section 4: Performance Review

The performance items in TODOS (NPU Tier 2/3) are themselves subjects of the review, not performance findings against the plan. New performance findings:

| Concern | Status | Risk |
|---|---|---|
| Memory budget at peak load (whisper-bindings + DistilBERT + Piper + RNNoise + ChromaDB) on 8GB M2 | unmeasured | Voice cloning would push it further; if peak > ~6GB, sentiment OOM kills with no fault |
| Sentiment classify latency (~150ms blocking on_message) | known per eng §2.8 | Back-to-back utterances queue. Under 10x visitor pressure: sentiment lags `tts/done` by seconds |
| t2v query timeout under Ollama+Chroma simultaneous load | unmeasured | If t2v silently timeouts, bush is silent; Codex CEO§2.9 + subagent F-series flag this |
| RNNoise CPU cost on M2 sustained | unmeasured | Possible thermal throttle under heat; not characterized |
| Boring-default fallback: name + measure | open item | "Vosk + espeak + no LLM-correct" performance characterized? |

**NEW TODO P1:** 1-hour bench-on-M2 in heated enclosure under continuous load. Capture RTF + memory residency + thermal throttle events. Writes to `~/.gstack/projects/FlamingBush-bushglue/bench-2026-XX-soak.json`.

### Eng Required Outputs

#### NOT in scope (eng phase deferrals)

| Item | Why deferred |
|---|---|
| Custom paho-mqtt fork / reconnect-debounce | Stock paho is fine for this load shape |
| Migration to MQTT v5 (session expiry, message expiry) | Mosquitto-only deploy; v3 is fine |
| Refactor of sentiment+t2v service boundaries | Out of scope; works as-is |
| Replacing CircuitPython with MicroPython on Pico | Out of scope; firmware works |

#### What already exists

(Per CEO §"What already exists" — same set; eng phase confirms no additional reuse opportunities discovered during code reads.)

#### Failure Modes Registry — UPDATED with eng findings

| Codepath | Failure mode | Rescued? | Test? | User sees | Logged? |
|---|---|:-:|:-:|---|:-:|
| `bush/<svc>/status` retained | Service crashes; stale `ready=true` outlives crash | **N CRITICAL** | N | bush-monitor lies | **N** |
| `bush_sentiment.on_message` | Daemon thread loop_forever exit | **N CRITICAL** | N | bush flat for hours | **N CRITICAL** |
| `bush_sentiment.classifier()` | Output shape mismatch (eng §9.A) | partial (`on_message` try/except) | **N** | bush flat | **N CRITICAL** |
| `bushutil.get_mqtt_broker()` | subprocess hang (WSL2) | **N** | N | services hang at startup | **N** |
| firmware `mqtt_open()` | 5s timeout during pulse | **N CRITICAL** | N | propane stuck up to 5s | **N** |
| `_run_legacy_iteration()` | Drift from baseline; silent regression | **N** | **N** | sentiment patterns shift | partial |
| `bush_stt.postprocess.correct_transcript()` | Hostile prompt injection | partial (length 3x check) | **N** | bush speaks malicious string | **N** |
| `bush-pray` cached 16k WAV | RNNoise misclassifies upsampled silent band | N | N | integration test green; real input red | N |
| t2v query timeout | Silent; no fallback verse | **N CRITICAL** | N | bush silent | **N** |
| Discord audio-recv | No retention policy; PII accumulates in WAVS_DIR | N | N | not user-visible; legal/policy risk | partial |

**Critical gaps after eng review: 6 (was 5; added stale-retained issue).** All flagged for inclusion in expanded TODOS at P0 level.

#### Worktree Parallelization Strategy

Implementation steps that can execute in parallel:

| Step | Modules touched | Depends on |
|------|----------------|------------|
| Pico force-OFF firmware fix | `firmware/relay-control/` | — |
| MQTT contract `bushutil.MqttServiceClient` | `packages/bushutil/` | — |
| `bush-pray` upsample-at-playback fix | `utils/bush-pray` | — |
| `bushutil.get_mqtt_broker()` timeout | `packages/bushutil/` | — |
| Per-service status migration (sentiment) | `services/sentiment/` | bushutil contract |
| Per-service status migration (audio/stt) | `services/audio/` | bushutil contract |
| Per-service status migration (core/tts, core/t2v) | `services/core/` | bushutil contract |
| Sentiment self-heal (paho retry) | `services/sentiment/` | bushutil contract |
| Sentiment §9.A verify | `services/sentiment/` | — |
| Integration test SUBACK + fake-MQTT seam | `utils/bush-integration-test`, `services/audio/tests/` | bushutil contract |
| Operator runbook | `docs/` | all of above (need to know the topic names) |
| Discord alert subscription | `services/discord/` | bushutil contract |
| Boring-default systemd overrides | `systemd/odroid/` | — |

**Lanes (parallel-safe groupings):**
- Lane A (firmware-only): Pico force-OFF
- Lane B (bushutil-only): MqttServiceClient + broker discovery timeout
- Lane C (utils-only): bush-pray fix, boring-default overrides
- Lane D (research): sentiment §9.A verify (30-min test)
- After Lanes B+D complete:
  - Lane E (sentiment): contract migration + self-heal + §9.A fix (sequential within lane)
  - Lane F (audio): contract migration + fake-MQTT seam regression test
  - Lane G (core): t2v + tts contract migration
  - Lane H (utils): integration test extension (depends on E,F,G)
- After all of above:
  - Lane I (docs): operator runbook
  - Lane J (discord): alert subscription

Conflict flag: Lanes E, F, G all touch the bushutil import surface; coordinate which file lands first.

Estimated CC+gstack time (excluding verification cycles): ~6-8 hours of focused engineering. Estimated wall-clock with verification deploys: 4-5 working days.

#### Eng Completion Summary

```
+========================================================================+
|                  ENG REVIEW — COMPLETION SUMMARY                       |
+========================================================================+
| Step 0: Scope Challenge | scope expanded per premise gate; 14-18 items |
| Architecture Review     | 0 new issues; MQTT contract shape locked     |
| Code Quality Review     | 8 issues with file:line; DRY → MqttServiceClient |
| Test Review             | diagram produced; 11 CRITICAL gaps; regression test mandatory |
| Performance Review      | 1 new P1 (bench-on-M2 soak); NPU defer confirmed |
| NOT in scope            | 4 items (paho fork, MQTT v5, refactor, MicroPython) |
| What already exists     | (cross-ref CEO phase)                         |
| TODOS.md updates        | total 17 items proposed (incl. CEO+eng deltas)|
| Failure modes           | 6 CRITICAL gaps                              |
| Outside voice           | ran (codex + Claude subagent)                |
| Parallelization         | 10 lanes (A-J), 4 stages of dependency       |
| Lake Score              | 16/18 recommendations chose complete option  |
+========================================================================+
```




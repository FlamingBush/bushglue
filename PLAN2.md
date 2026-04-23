# Plan: Motorized Needle Valve for Mid-Pressure Flame Modulation

## Context

**bushglue** is the software for *AI Am / Burning Bush*, an interactive installation in which AI personalities speak through fire. The current signal pipeline is:

```
bush-stt → bush-t2v → bush-tts + bush-sentiment → fire / audio
```

Listeners speak to the bush; STT converts their speech to text; t2v does semantic retrieval from ChromaDB; TTS synthesizes a response (espeak + sox reverb); sentiment analysis runs over either the input, the response, or both. An audio-agent plays the synthesized voice. A fire-control subsystem (currently `firmware/relay-control/CIRCUITPY/code.py` on a Pico 2 W) drives solenoids for a poofer with an accumulator.

**This plan adds a new actuator:** a stepper-motorized 1/4" NPT brass needle valve that modulates a separate mid-pressure propane line. Unlike the poofer, which is binary and explosive, this valve provides smooth, expressive, sub-Hz to ~2 Hz flame-size modulation. It is driven by the bush's sentiment and speech state — flame size tracks the bush's emotional/rhetorical intent rather than audio envelope. Think "flame as breath, held high during proclamations, dropping during silences" rather than "flame as VU meter."

The valve is a Litorange-brand (or similar cheap import) 1/4" NPT brass needle valve. The knurled knob is permanently affixed (brazed or epoxied — does not come off). The valve never fully seals, which is fine because it is not part of any safety shutoff pathway; a separate upstream solenoid handles shutoff.

## Hardware Summary (Out of Scope for Implementation, Documented for Context)

The physical build, motor selection, mounting, and deployment hardening are the human's responsibility and out of scope for this plan. Documented here so the firmware makes sense.

- **Valve:** 1/4" NPT brass needle valve, Litorange or equivalent. ~4–8 turns lock-to-lock. Knob is permanent.
- **Actuator:** MKS SERVO42C-MT V1.1 — closed-loop integrated NEMA 17 servo stepper with motor included. Onboard STM32, magnetic encoder, and driver. Accepts step/dir or UART/CAN commands. Self-contained — no external stepper driver board.
- **Coupling:** 3D-printed cup that grips the knob's knurled OD. Cup mates to the motor shaft via a lift-off dog clutch so the motor can be removed without tools for manual operation (motors/drivers will fail at Burning Man due to conductive alkaline dust; manual override must be toolless).
- **Mounting:** 3D-printed two-piece clamp around the 41mm hex body of the valve, with a tower/plate supporting the motor concentric with the stem. Spring-loaded axial compliance lets the motor float as the knob rises and falls with valve position (the stem rises with the thread pitch as the valve opens).
- **Control philosophy:** Stall-as-fuse. Motor torque is deliberately limited (via driver current setting) such that the motor stalls before it can damage the valve seat or bonnet threads. Homing is always toward the fully-open (soft) stop, never toward the sealing needle seat.

## Architecture Decisions (Already Made)

These are fixed for this plan. Do not revisit.

1. **Dedicated stepper MCU.** The integrated servo has its own STM32 onboard. The main Pico 2 W does not generate step pulses or handle real-time motor control.
2. **Pico ↔ stepper interface is UART.** Text-based protocol, human-readable, debuggable with a serial terminal. Not I2C, not SPI, not step/dir.
3. **Pico subscribes to MQTT for valve commands** and translates them to UART commands. Pico publishes valve position feedback and status back to MQTT.
4. **Homing direction: open.** The valve is homed by driving toward the fully-open mechanical stop (metal-on-metal, non-sealing). Never toward the needle seat. Soft limits in firmware prevent driving into the seat.
5. **Stall detection is a secondary safeguard, not a primary endstop.** Closed-loop encoder reports position errors; firmware treats a sustained position error as a stall event and halts.
6. **Firmware that runs on an MCU lives under `firmware/`.** New stepper-controller firmware (if any custom firmware is written for the integrated servo; most likely not needed since stock firmware is adequate) goes in `firmware/valve-control/`. New Pico code for valve command translation is added to the existing `firmware/relay-control/CIRCUITPY/code.py` if it's simple, or to a new module in that same directory if it's substantial. Do not create a separate Pico firmware for the valve — reuse the existing relay-control Pico.
7. **Host-side code lives in `services/`.** Integration of valve commanding into the sentiment-driven flame logic is a service, following the pattern of existing `services/sentiment`.

## Why These Decisions (Reasoning for Self-Containment)

- **Why closed-loop integrated servo over discrete stepper + TMC2209:** Dust environment means friction varies day to day; closed-loop reports missed steps and recovers. Integrated package is one sealed unit to deploy and replace. Slightly more expensive but much more reliable in the field.
- **Why NEMA 17 over NEMA 14:** Availability and ecosystem (every hobbyist integrated servo is primarily NEMA 17), more torque headroom for stall-based homing with margin, better detent torque for holding position when unpowered, better heat dissipation, standard 31mm mounting pattern. Size/weight don't matter for this installation.
- **Why UART over step/dir:** We need position feedback from the encoder back to the Pico, and UART gives us that in the same interface as commands. Step/dir is one-way.
- **Why home open:** The open stop is a soft mechanical stop (stem bottoms out in the bonnet threads). The closed stop is a sealing surface that plastically deforms under load — driving a motor into it repeatedly damages the valve's low-end regulation. Homing open and using counted steps plus soft limits for all other positions preserves the valve.
- **Why let the main Pico do MQTT-to-UART translation:** Keeps the integrated servo stock-firmware and replaceable. Keeps all Wi-Fi, MQTT, and bush-awareness logic in one place. Pico has ample headroom for this.
- **Why reuse the existing relay-control Pico rather than adding a second Pico:** Simpler deployment (one board, one firmware, one set of spare parts). This valve is not safety-critical, so the failure-domain argument for separation doesn't apply. If the existing Pico is already at its CPU/memory ceiling, reconsider — but verify first rather than assuming.
- **Why sentiment-driven and not audio-envelope-driven:** The needle-valve + downstream-pipe-volume + nozzle system has a physical bandwidth limit of roughly 1–2 Hz for meaningful amplitude changes. Audio envelope is much faster than this and trying to track it produces visual mush. Sentiment changes slowly (per-utterance or per-phrase), which matches the valve's physical capabilities and reads as intentional rather than reactive. This is the right signal for this actuator.

## What Claude Code Needs to Do

### 1. Read the existing code to understand what's there

Before writing anything, read and understand:

- `firmware/relay-control/CIRCUITPY/code.py` — current Pico firmware. Understand: MQTT topic naming conventions, connection/reconnection logic, main loop structure, currently used GPIO pins, memory usage (if discernable), imports and libraries already present.
- `services/sentiment` (directory) — what signals the sentiment service publishes, what MQTT topics it uses, what granularity and rate of updates it emits, what data types (scalar float? categorical label? vector?).
- `MQTT.md` at repo root — canonical MQTT topic namespace and conventions.
- `PROJECT.md` — high-level vision, useful for understanding naming and tone.
- `mqtt-architecture.dot` / `mqtt-architecture.svg` — visual architecture. Update this diagram to include the new valve subsystem as part of the implementation.
- Top-level Python services (`audio-agent.py`, `bush`, `bush-firecontrol`, etc.) — to understand how services are structured, systemd integration patterns, logging conventions.
- `systemd/` directory — existing unit files, pattern for new services.
- `INSTALL.md` — update to include any new setup steps for the valve subsystem.

**Do not invent naming conventions.** Mirror what's already in the repo. If the existing code uses `bush/<subsystem>/<action>`, follow that. If it uses `bush-<subsystem>-<action>`, follow that.

### 2. Extend the Pico firmware (`firmware/relay-control/CIRCUITPY/code.py`)

Add valve control to the existing Pico without breaking relay control. Concrete changes:

- **Initialize a UART** on two free GPIO pins. Check the existing code to identify which pins are unused. Use `busio.UART` at 115200 baud, 8N1, no flow control. Use a reasonable timeout (e.g., 100ms) for read operations.
- **Define the UART protocol.** Text-based, newline-terminated commands and responses. Proposed commands (adjust if needed to match integrated-servo firmware the user ends up choosing; these are placeholders):

  | Command | Response | Purpose |
  |---|---|---|
  | `MOVE <steps>\n` | `OK\n` or `ERR <reason>\n` | Move to absolute step position |
  | `POS?\n` | `POS <steps>\n` | Query current position |
  | `HOME\n` | `OK\n` when complete, `ERR <reason>\n` on failure | Run homing routine (drive to open stop, back off, zero) |
  | `STATUS?\n` | `STATUS idle\|moving\|homing\|stalled pos=<n> target=<n>\n` | Query full status |
  | `STOP\n` | `OK\n` | Abort current motion, hold position |
  | `LIMIT <min> <max>\n` | `OK\n` | Set soft position limits |
  | `CURRENT <percent>\n` | `OK\n` | Set motor current as percent of rated (for stall-torque tuning) |

  Document the actual protocol chosen in a new file `firmware/valve-control/PROTOCOL.md` (see step 4). The Pico's implementation must match whatever the integrated servo firmware accepts; if the stock firmware differs, translate. If stock firmware is serial-compatible (most MKS/BTT units accept a Marlin-like G-code subset), use that directly and adapt the abstraction in the Pico code.

- **Add MQTT subscriptions:**
  - `bush/fire/valve/target` (or namespace-appropriate equivalent) — float 0.0–1.0 representing fractional valve opening (0 = closed/minimum, 1 = fully open). Pico converts to step position using a calibrated `open_steps` constant and sends MOVE.
  - `bush/fire/valve/home` — any message triggers homing
  - `bush/fire/valve/stop` — any message triggers immediate STOP
  - `bush/fire/valve/calibrate` — payload is the number of steps from open-stop to closed-soft-limit; updates the `open_steps` calibration at runtime (persisted? or just until reboot — specify).

- **Add MQTT publications:**
  - `bush/fire/valve/actual` — float 0.0–1.0, current fractional position, published at a reasonable rate during motion (e.g., 5 Hz during moves, once per second when idle)
  - `bush/fire/valve/status` — JSON or simple key=value, published on state changes: `{"state": "idle|moving|homing|stalled|error", "pos": 0.42, "target": 0.50, "stalled": false, "last_error": null}`
  - `bush/fire/valve/online` — retained birth/LWT message for availability tracking

- **Slew limiting / command smoothing.** The sentiment service may publish new targets faster than the valve can realistically follow. Smoothing rules:
  - Rate-limit target updates to no more than 10 Hz (drop or coalesce faster updates)
  - Never command a move that would require the motor to run faster than `max_step_rate` (configurable constant; start with 800 steps/sec at 1/16 microstepping for smooth motion)
  - Coalesce pending targets: if a new target arrives while moving, update the in-flight target rather than queueing moves

- **Soft limits.** Enforce in the Pico before sending MOVE to the stepper. Track `open_steps` (the known span from home-open to software-closed) as a configurable constant with a sensible default. Never send MOVE with a step count outside `[closed_soft_limit, 0]` where 0 is the home-open position and negative values move toward closed. Sign convention is arbitrary but pick one and stick to it — document it.

- **Error handling:**
  - UART read timeout → log, publish error to `status`, attempt one retry, then mark valve as offline
  - UART reports stall → halt further moves, publish stalled state, require explicit rehome to clear
  - MQTT disconnect → continue running last commanded position, reconnect per existing relay-control's reconnection logic, republish online status on reconnect
  - Startup with unknown position → do not move until homed; reject MOVE commands with `ERR not_homed` until HOME completes

- **Structure.** If the existing `code.py` is small (<300 lines), add the valve logic inline with clear section comments. If it's already substantial, extract valve logic to a new module `firmware/relay-control/CIRCUITPY/valve.py` and import it from `code.py`. Match the style of the existing code.

### 3. Extend the sentiment service (or add new service) to drive the valve

Read `services/sentiment` first to understand its current output. Then decide whether to extend it or add a new service `services/flame-expression` (or similar) that subscribes to sentiment and publishes to `bush/fire/valve/target`. Prefer the separate-service approach if the sentiment service is a pure analyzer; prefer extending it if it already publishes actuator-relevant signals.

The logic should:

- **Map sentiment state to baseline valve position.** Specifics depend on what the sentiment service produces. Suggested mapping if it outputs a valence score in [-1, 1]:
  - -1 (sad/quiet) → 0.1 baseline (small flame, barely alive)
  - 0 (neutral) → 0.4 baseline (calm presence)
  - +1 (angry/intense) → 0.9 baseline (large flame)
  - Use slow LFO-like modulation around the baseline (e.g., ±0.05 at 0.2 Hz) so the flame "breathes" even when sentiment is static

- **Modulate baseline with TTS speech state.** While TTS is actively playing:
  - Rise slightly from baseline at the start of utterance (e.g., +0.1 over 200ms)
  - Decay back during pauses between phrases
  - Drop below baseline briefly during silences (e.g., -0.15 for silences longer than 400ms)
  - Return to sentiment-driven baseline after utterance completes

  If TTS word-timing info is available from `tts-service.py`, use it. If not, use a simple speaking/silent binary from audio-agent's playback state.

- **Handle transitions gracefully.** When sentiment changes between utterances, ramp the baseline over 1–2 seconds rather than stepping. This is the flame "settling into" the new mood.

- **Publish to `bush/fire/valve/target` at 10 Hz.** Let the Pico firmware do slew limiting to physical capabilities; the service just needs to produce a smooth target signal.

- **Fail safely.** If the sentiment service loses its input, revert to a default baseline (0.3 or similar — visible but modest flame). Do not let the valve target become stale-frozen indefinitely; publish at least a heartbeat at the nominal rate.

### 4. New documentation

- **`firmware/valve-control/PROTOCOL.md`** — document the UART protocol between the Pico and the MKS SERVO42C-MT V1.1. Include the command table, examples, error codes, and any quirks of the MKS firmware. Consult the MKS GitHub documentation (`makerbase-mks/MKS-SERVO42C` or the current canonical repo) for the actual command syntax; the command table in section 2 is a conceptual placeholder and must be reconciled against what the SERVO42C actually accepts.
- **`firmware/valve-control/CALIBRATION.md`** — how to determine `open_steps` for a specific valve (procedure: home open, manually count hand turns from open to closed, compute steps-per-turn, multiply). How to adjust the motor current to get the right stall torque. How to test homing.
- **`MQTT.md`** — add the new valve topics to the existing MQTT topic reference. Follow existing formatting.
- **`mqtt-architecture.dot` and `.svg`** — add the new valve subsystem nodes and edges to the architecture diagram. Re-render the SVG using the existing `render-diagram.py` script (read that script first to understand its invocation).
- **`INSTALL.md`** — add any setup steps: new systemd service file to enable, Pico firmware update procedure, integrated servo initial configuration.
- **`systemd/`** — add a new unit file for the flame-expression service (or modify the sentiment service unit if extending in place). Follow the pattern of existing unit files.
- **`README.md`** — add the valve to the "Tweaking" table with its knob file location.

### 5. Testing / validation

Write simple test scripts or CLI commands (not a heavy test framework — this is a small project) that the human can use to validate the build at each stage:

- **UART loopback test** — script that opens the Pico's UART and exchanges a known command with the integrated servo, prints the response. Usable from a laptop via USB-serial for bring-up, before the Pico has the full firmware flashed.
- **`bushctl valve`** subcommand (or extend existing `bush` CLI): `bushctl valve target 0.5`, `bushctl valve home`, `bushctl valve status`, `bushctl valve stop`. Publishes to the right MQTT topics and prints responses. Follow the pattern of the existing `bushctl` subcommands (see `bush` script).
- **Pipeline smoke test** — extend `bush-integration-test` to include valve homing and a small move-and-return cycle. Fail fast if the valve is offline or doesn't respond.

## Open Questions / Assumptions to Verify

Do not proceed with assumptions on these — check the repo and flag or resolve:

1. **Free GPIO pins on the Pico.** Read `code.py` and determine which pins are currently used for relay control. Pick two adjacent unused pins for UART TX/RX. Document the choice.
2. **Existing MQTT topic conventions.** Confirm `bush/fire/*` is the right namespace, or adopt whatever the existing code uses. Same for message payloads (raw values vs. JSON).
3. **Sentiment service output.** What does it actually publish? Scalar, categorical label, or richer structure? This determines the mapping in step 3.
4. **TTS word-timing.** Does `tts-service.py` publish word or phoneme timing on MQTT, or only overall start/end events? Adjust the speech-modulation logic accordingly.
5. **Pico firmware version and library availability.** What CircuitPython version is the Pico running? Is `busio.UART` available and adequate? Is there enough flash space for the added code?
6. **Is there a way to persist `open_steps` calibration across Pico reboots?** CircuitPython's NVM or a small JSON file on CIRCUITPY. Check whether the existing code does this for anything else.
7. **Integrated-servo model:** MKS SERVO42C-MT V1.1. Firmware protocol is vendor-specific — read MKS documentation (check their GitHub: `makerbase-mks/MKS-SERVO42C`) to determine the actual command syntax before implementing. Write the Pico side in a way that isolates the vendor-specific command strings behind a small abstraction (e.g., a `StepperProtocol` class with `move()`, `query_position()`, `home()`, `stop()` methods) so firmware differences or a future hardware swap is a one-file change.

## Explicitly Out of Scope

- Physical mounting, bracket design, coupler design, motor selection, motor ordering, stepper current tuning (these are the human's responsibility)
- Hardware dust-sealing, conformal coating, cable glanding, enclosure design
- Safety shutoff pathway (handled by separate upstream solenoid, not this valve)
- Poofer timing and logic (already implemented in relay-control)
- TTS voice quality, sentiment accuracy, or STT improvements (separate services, not affected by this change)
- Any change to the audio pipeline beyond reading existing state
- Power supply sizing or 12V/24V bus design

## Failure Modes to Handle Gracefully

| Failure | Expected Behavior |
|---|---|
| UART timeout / stepper MCU unresponsive | Log, publish error status, mark valve offline in MQTT, retry connection periodically. Do not crash the relay-control main loop. |
| Stepper reports stall mid-move | Halt further moves, publish stalled state, require explicit `home` or `calibrate` command to clear. |
| MQTT broker disconnect | Continue holding last commanded position. Reconnect per existing Pico logic. Republish valve online+status on reconnect. |
| Pico reboot mid-move | Stepper holds last position (integrated servo retains position in its own encoder). On boot, Pico marks valve as "unknown position" and requires rehoming before accepting MOVE commands. |
| Sentiment service crash / disconnect | Flame-expression service detects stale input, reverts valve target to default baseline (e.g., 0.3). Heartbeats continue at nominal rate. |
| Sentiment-service publishes garbage (NaN, out-of-range) | Clamp to [0, 1], log warning, continue. |
| MQTT message storm (faster than valve can follow) | Slew limit in Pico firmware. Drop/coalesce targets. No queue buildup. |
| Power brown-out | Integrated servo's onboard brownout handling takes over. Pico restarts per its own watchdog. On recovery, valve starts in unknown-position state. |
| Hand-operation while motor is disengaged | When motor is re-engaged, Pico should re-home before accepting new targets. Provide a clear `rehome` command path. |
| Conductive dust shorts on the integrated servo | Hardware problem, out of scope for this plan, but document the symptom (stepper doesn't respond) in `CALIBRATION.md` troubleshooting section. |

## Implementation Order

Recommended sequence to avoid rework:

1. Read all existing files listed in section 1. Produce a short summary in the chat confirming the current architecture and resolving the open questions that can be answered from code.
2. Flag any open questions that require the human to answer (stepper model, etc.) before proceeding.
3. Extend Pico firmware: UART setup, protocol abstraction, MOVE/HOME/STATUS, MQTT topics, soft limits. Test with loopback before connecting to real hardware.
4. Add new documentation: PROTOCOL.md, CALIBRATION.md, MQTT.md entries, architecture diagram update.
5. Implement flame-expression service (new or extension of sentiment). Start with a simple sentiment→baseline mapping; add speech modulation once baseline works.
6. Add systemd unit and `bushctl` commands.
7. Extend integration test.
8. Final pass on INSTALL.md and README.md.

## Style Notes

- Match the existing repo's code style (imports, naming, logging). Don't import a new logging framework if one's already in use.
- Prefer small functions and clear names over clever abstractions.
- Comments should explain *why*, not *what*. Assume the reader knows Python and MQTT.
- All new user-facing text (CLI help, error messages, log lines) should be consistent with the bush/AI Am voice — functional and clear, not ornate.

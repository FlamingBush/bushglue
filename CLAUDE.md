# bushglue

## Agent contribution norms

This repo is maintained by one meatbag at a time. Agent contributions land here from multiple sources. To keep the signal-to-decoration ratio high:

- **Default to updating existing docs only as necessary.** If the change is self-evident from the diff + commit message, don't write a 1-pager. Focus on user-facing critical info only.
- **One doc per change, not a fan-out.** No separate 1-pager + walkthrough + full review + amendment for the same work. Pick one.
- **Don't invent decisions for a fictitious team.** There is one meatbag reading. Address them directly. No "the team needs to decide X" sections, no "Ask:" closers, no rhetorical decision lists.
- **Risk/severity tables** only when there are more than three real risks. Two risks fit in two sentences.
- **Write to the operator, not to a stakeholder.** Skip the executive-summary register. Plain technical prose.

## Valve control — where it stands (2026-06-06)

On `main`: the valve node is a **Pico 2 W driving the MKS SERVO42D as a plain stepper over STEP/DIR** — no CAN, no UART, no encoder feedback (the MCP2515 CAN controller died; CAN is abandoned). The Pico toggles STEP/DIR straight from GPIO (STEP=GP4, DIR=GP5, EN unwired) and the 42D closes its own loop to the pulses. Position is **dead-reckoned** from the step rate (`_set_velocity` drives a `pwmio` PWM whose frequency = steps/sec); breath, the bush-cue stream, and target moves all run through it. 16× microstep, de-energized when idle. Required 42D menu: Mode=CR_vFOC, MStep=16, En=Hold. Wi-Fi/MQTT and the bush-cue stream framing are unchanged. Protocol details in `firmware/valve-control/PROTOCOL.md`.

VERIFIED on the bench 2026-06-06: boots clean, joins Wi-Fi, jogs via step/dir (DIR toward open confirmed), connects to MQTT, and a full `bush/fire/valve/target` round-trip drives the needle. Still **UNCALIBRATED**: `OPEN_STEPS=2000` is a placeholder — set real travel via `bush/fire/valve/calibrate <steps>`.

With no encoder, **there is no homing**: boot position = 0 dead-reckoned at wherever the shaft sits, and `home` just re-declares the current position as 0. Seat safety depends on not commanding a stroke into the force-sensitive seat plus a calibrated `OPEN_STEPS`. A loose coupler no longer fakes telemetry, but the needle still won't follow the reckoned position — keep it uncoupled or hand-parked mid-travel on the bench. More in the MKS-quirks and closed-seat memories.


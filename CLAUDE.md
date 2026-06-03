# bushglue

## Agent contribution norms

This repo is maintained by one meatbag at a time. Agent contributions land here from multiple sources. To keep the signal-to-decoration ratio high:

- **Default to updating existing docs only as necessary.** If the change is self-evident from the diff + commit message, don't write a 1-pager. Focus on user-facing critical info only.
- **One doc per change, not a fan-out.** No separate 1-pager + walkthrough + full review + amendment for the same work. Pick one.
- **Don't invent decisions for a fictitious team.** There is one meatbag reading. Address them directly. No "the team needs to decide X" sections, no "Ask:" closers, no rhetorical decision lists.
- **Risk/severity tables** only when there are more than three real risks. Two risks fit in two sentences.
- **Write to the operator, not to a stakeholder.** Skip the executive-summary register. Plain technical prose.

## Valve control — where it stands (2026-06-03)

On `main`: valve position is **encoder-grounded** (absolute, via the MT `0x30` encoder) — moves, nudges, and homing re-read the encoder instead of dead-reckoning, so position can't drift. Breath is a **valley-grounded smooth `0xF6`** (re-grounds at the bottom of each cycle; drift-free over a 4.5-min soak). 16× microstep; homes into the closed seat (gentle inchworm), de-energized when idle. Protocol details in `firmware/valve-control/PROTOCOL.md`.

Paused mid-experiment on branch **`valve-64x-mood-microstep`** (UNVALIDATED): 64× microstep for slower smooth breaths, intended to support mood-driven slow/fast microstep switching, plus a phase re-anchor to remove the ~0.05 breath-center offset. These were only ever tested while a coupler was loose, so the results are untrustworthy — re-verify from scratch.

⚠️ **Hardware:** the heat-press-fit motor coupler came loose 2026-06-03. The encoder is on the motor shaft, so a loose coupler reads motor rotation while the needle doesn't follow → garbage position telemetry (it faked a "homing into the seat" and a breath "runaway"). After re-fitting it, **re-home** (the zero moves) and re-confirm the breath before trusting any encoder-derived behavior. More in the MKS-quirks memory.


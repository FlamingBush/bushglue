# Plan: inchworm (kiss-and-back-off) homing for the needle valve

**For the implementing agent.** This replaces the ram-the-stop homing with a gentle
inchworm approach. All of the constraints below were learned the hard way on the bench —
trust them, don't re-derive. File to change: `firmware/relay-control/CIRCUITPY/valve.py`
(CircuitPython on the Pico 2 W). Keep edits terse; this repo strips explanatory comment
blocks (default to no comments).

## Objective

Homing must approach the stop **gently and stop at first contact**, never ramming. Today
`cmd_home()` fires `0x94` RETURN_ZERO, which drives continuously into the stop; the MKS
returns no encoder reads while it's driving/stalled, so the deadband detector can't fire
until a read finally gets through (~5 s) — the motor rams the stop at ~1.6 A the whole
time. Measured on the bench: **1.6 A for ~5 s every home.** Unacceptable, and destructive
on the force-sensitive closed seat.

## Scope

- **IN:** replace the `0x94` ram-homing with an inchworm approach. Keep `HOME_DIR = open`
  (homes toward the *tolerant* open stop). Validate on the open stop.
- **OUT (do NOT do here):** switching `HOME_DIR`/position convention to the closed seat
  (separate follow-up). The breath redesign. Do not flip to the seat until the inchworm
  is proven on the open stop.

## Hard constraints (field-confirmed — do not violate)

1. **No reliable encoder reads during motion.** The MKS (`0x30`) returns nothing while
   executing `0x94`/`0xF6` or while stalled-and-driving. Reads ARE reliable when the motor
   is **stopped** (between `0xFD` moves). This is why the inchworm uses discrete `0xFD`
   steps with reads *between* them.
2. **`0xFD` is the good primitive.** Closed-loop, reaches its relative target, stops, and
   returns a clean two-stage ACK (status=1 start, status=2 complete; status=0 = stall).
   It returns status=2 even if it physically couldn't move (rammed) — so detect contact
   by the **encoder**, not the status.
3. **No working torque cap.** `0x83` SET_CURRENT is ignored in CR_UART. `0xA5`
   SET_MAX_TORQUE did NOT cap on the bench (512 still gave 1.6 A; runtime likely ignored).
   So **do not rely on a torque cap for gentleness** — the inchworm's stop-at-first-contact
   IS the gentleness. (`0xA5`/`maxtorque` plumbing exists; leave it, don't depend on it.)
4. **Counts/microstep is uncertain** (~17.5–20.5, varies run to run). So contact detection
   must be **self-calibrated** (relative to the observed free-motion per-step delta), not a
   hardcoded expected-counts value.
5. **Cooperative loop.** `valve.service()` runs alongside sub-ms solenoid deadlines; it
   must stay non-blocking. The inchworm must be **ACK-driven** (one step per response
   cycle, like the existing chain), never a blocking loop or sleeps.
6. **Open stop is tolerant; closed seat is force-sensitive** (rams jam it, needing a power
   cycle + hand-unjam). Test only on the open stop in this change.

## Current code map (what to reuse vs replace)

- `cmd_home()` — starts homing; resets `_home_*` vars; sends `CMD_CLEAR_PROTECT`.
- Homing chain in `_parse_response()`: `home_clear_protect → home_protect_off → home_zmode
  → home_zdir → home_zspeed →` fires `0x94` (no ACK). **Replace the `zmode/zdir/zspeed/0x94`
  tail with the inchworm.** `home_clear_protect` + `home_protect_off` can stay.
- `service()` `state=="homing"` branch: polls `0x30` every `HOME_POLL_MS`, deadband stall
  via `_home_last_move_ms`/`HOME_STALL_DEADBAND`. **Remove this poll-based detection** (the
  inchworm detects per-step instead). **Keep** `HOME_TIMEOUT_MS` as a backstop that
  de-energizes (`cmd_stop()` + `ENABLE 0`).
- `_complete_homing_by_stall()` → sets `state="homing_finalize"`, de-energizes. **Reuse as
  the contact handler** (call it when the inchworm detects contact).
- `_service_finalize()` + `home_backoff_start`/`home_backoff_done` handlers + `_finish_homing()`
  — the **back-off + SET_ZERO-off-the-stop sequence already works. Reuse it unchanged.**
- The `nudge` handlers (`nudge_start`/`nudge_done`, raw relative `0xFD`) show the exact
  pattern for an un-clamped relative `0xFD` move + two-stage ACK. The inchworm step is the
  same shape.
- The `read_encoder` 8-byte parse branch (`cmd == "read_encoder"`): extend it to also handle
  the inchworm reads (`home_inch_seed`, `home_inch_read`) — same 8-byte frame, branch on
  `cmd`.
- Constants present: `MOVE_SPEED`, `DIR_TOWARD_OPEN`/`DIR_TOWARD_CLOSED`, `HOME_DIR`,
  `HOME_BACKOFF_STEPS`, `HOME_TIMEOUT_MS`, `CMD_MOVE_POS`, `CMD_READ_ENCODER`, `CMD_ENABLE`.

## Implementation

### New constants (near the other `HOME_*`)
- `HOME_INCH_STEPS = 200` — microsteps per inch step (~22.5° at 16× / 3200-per-rev).
- `HOME_INCH_SPEED = 2` — `0xFD` speed gear for the approach (slow; gentle contact).
- `HOME_CONTACT_FRAC = 0.4` — a step that advances < this fraction of the learned cruise
  delta is "contact".
- `HOME_INCH_MAX = 160` — safety bound on steps (≈ full travel / step + margin).

### New state vars
- `_home_prev_raw` (int or None) — last encoder raw.
- `_home_cruise = 0` — max per-step |delta| seen (free-motion baseline, self-calibrated).
- `_home_inch_count = 0`.
Reset all in `cmd_home()`.

### Chain rewrite
`home_protect_off` handler: instead of `home_zmode`, send `CMD_ENABLE 0x01`
(expect `"home_enable"`) to guarantee the motor is energized for the `0xFD` steps.

`home_enable` handler: send `CMD_READ_ENCODER` (expect `"home_inch_seed"`).

`home_inch_seed` (in the 8-byte read branch): `_home_prev_raw = raw`; then issue the first
inch step (helper `_home_issue_inch()`).

`_home_issue_inch()`:
```
direction = HOME_DIR is 0x00 -> DIR_TOWARD_OPEN  (keep using HOME_DIR semantics)
speed_dir = direction | (HOME_INCH_SPEED & 0x7F)
_send_and_expect([CMD_MOVE_POS, speed_dir] + HOME_INCH_STEPS.to_bytes(4,"big"), "home_inch_start")
```

`home_inch_start` (3-byte): status==1 → `_pending_cmd="home_inch_done"`; else → error+de-energize.

`home_inch_done` (3-byte): status==1 → ignore (stray, like move_done does); else (2 or 0) →
send `CMD_READ_ENCODER` (expect `"home_inch_read"`). (Don't trust status for contact.)

`home_inch_read` (8-byte read branch):
```
delta = abs(raw - _home_prev_raw); _home_prev_raw = raw
_home_inch_count += 1
if delta > _home_cruise: _home_cruise = delta
contact = (_home_inch_count >= 2 and _home_cruise > 0
           and delta * 1.0 < _home_cruise * HOME_CONTACT_FRAC)
if contact:
    _complete_homing_by_stall()      # reuse: de-energize -> finalize backs off + SET_ZERO
elif _home_inch_count >= HOME_INCH_MAX:
    # never found the stop -> abort safely
    cmd_stop(); _send([CMD_ENABLE,0x00]); state="error"; last_error="home_no_contact"
else:
    _home_issue_inch()               # next step
print(f"Valve: inch {_home_inch_count} raw={raw} d={delta} cruise={_home_cruise}")  # keep
                                                                       # this print for tuning
```
Require `_home_inch_count >= 2` so the first step establishes the cruise baseline before
the relative test can fire. (`HOME_CONTACT_FRAC` is self-calibrated against `_home_cruise`,
so it's robust to the unknown counts/microstep.)

`_check_timeout()`: add `"home_inch_done"` to the `MOVE_TIMEOUT_MS` set (it's a `0xFD`
move); `home_inch_start`/`home_inch_seed`/`home_inch_read` use the default `CMD_TIMEOUT_MS`.
Leave the existing de-energize-on-timeout behaviour.

`service()` `state=="homing"`: keep only the `HOME_TIMEOUT_MS` backstop (de-energize +
error). Delete the `0x30` poll + deadband stall block (now handled per-step).

Delete the now-unused `home_zmode`/`home_zdir`/`home_zspeed` handlers and the `0x94` fire.
Leave `CMD_RETURN_ZERO`, `CMD_SET_ZERO_*` constants if other code references them; otherwise
remove.

## Deploy + observe loop (Pico is on THIS laptop now)

- **Drive:** `/Volumes/CIRCUITPY/` — deploy with
  `cp firmware/relay-control/CIRCUITPY/valve.py /Volumes/CIRCUITPY/valve.py && sync`
  (triggers CircuitPython auto-reload). macOS write to a CIRCUITPY drive is finicky — after
  copying, run `python3 -m py_compile /Volumes/CIRCUITPY/valve.py` to confirm it isn't
  truncated/corrupt.
- **Console:** `/dev/cu.usbmodem1101`, 115200. Read with a short pyserial loop (reads only
  return when the firmware prints; the per-step `inch ...` print above is your main signal).
- **MQTT:** localhost broker (mosquitto running on `*:1883`, anonymous; a pong responder is
  running so the Pico's scan latches on). Pico is at `192.168.1.147`, broker `192.168.1.129`.
  - Home: `mosquitto_pub -h localhost -t bush/fire/valve/home -m ""`
  - Status: `mosquitto_sub -h localhost -t bush/fire/valve/status -C 1 -W 6`
  - Existing debug topics: `bush/fire/valve/nudge` (signed degrees, raw relative move),
    `bush/fire/valve/maxtorque` (0..0x4B0).
- After a reload, the Pico must reconnect WiFi+MQTT (~10–15 s) before it answers.
- Foreground `sleep` is blocked in this shell; use the Bash tool's own timeout, or pyserial
  with `time.sleep` inside python.

## Validation (open stop only — the operator reads the supply meter)

1. Confirm `init OK` and idle on the console after deploy.
2. Send `home`. On the console watch the `inch ... d=... cruise=...` line: `d` should be
   large and steady (cruise) for many steps, then **drop sharply on one step (contact)**,
   immediately followed by `entering finalize (will back off)` → `backing off` → `homed`.
3. **Operator:** confirm on the meter there is **no sustained high current** — contact is a
   brief blip (one slow step), then de-energize + back-off. The old 5 s / 1.6 A ram must be
   gone.
4. Tune from the observed deltas: if it false-trips mid-travel, lower `HOME_CONTACT_FRAC`;
   if it overshoots into the stop too hard, lower `HOME_INCH_STEPS` and/or `HOME_INCH_SPEED`.
   Re-home from a few different start positions (use `nudge` to move it first).

## Acceptance criteria

- Homing completes reliably from several start positions, ending `homed`, idle, zero set
  ~`HOME_BACKOFF_STEPS` off the stop.
- Per-step deltas on the console show a clear cruise→contact drop; contact is detected
  within one inch step.
- Operator confirms **no multi-second high-current ram** — contact current is a brief blip.
- `py_compile` clean; no regressions to normal moves/targets.

## Safety (must)

- Keep `HOME_DIR = open`. Do **not** point the inchworm at the closed seat in this change.
- Small steps, slow speed — a contact step must be gentle even though `0xA5` won't cap it.
- Operator watches the meter; any sustained high current → stop, de-energize, kill power.
- See memory: closed seat is force-sensitive; supply must run ~2 A (under-current browns
  out the controller). Don't lower the supply as a "fuse".

# valve.py -- Motorized needle valve via MKS SERVO42C-MT V1.1.2
# UART binary protocol on GP4 (TX) / GP5 (RX) at 115200 baud.
#
# Position convention (motor steps):
#   motor_pos_steps = 0           -> fully closed (homing seat margin, MKS zero set here)
#   motor_pos_steps = open_steps  -> fully open
# MQTT 0.0 = closed, 1.0 = open. motor_pos_steps = target * open_steps.
#
# Architecture: the SERVO42C is a closed-loop FOC servo with an absolute
# multi-turn encoder (0x30) on the MT variant. motor_pos_steps is DERIVED from
# that encoder, never dead-reckoned: homing captures the zero + sign, then every
# move, nudge, and breath tick re-reads 0x30 so estimation error can't accumulate
# (that accumulation was the breathe drift). 0xFD still reports completion via its
# status=2 ACK (two responses: status=1 starting, status=2 complete; see manual
# §5.5.5 / §6.4); we read the encoder right after to ground the new position.

import board
import busio
import supervisor
import json
import math
import struct
import time

uart = busio.UART(board.GP4, board.GP5, baudrate=115200, timeout=0.1)

# ── MKS SERVO42C V1.1.2 protocol ───────────────────────────────────────────
MKS_ADDR            = 0xE0
CMD_READ_ENCODER    = 0x30   # -> 8B: addr + int32 carry + uint16 value + crc
CMD_CLEAR_PROTECT   = 0x3D
CMD_READ_PROTECT    = 0x3E
CMD_SET_MODE        = 0x82
CMD_SET_CURRENT     = 0x83
CMD_SET_MICROSTEP   = 0x84
CMD_SET_PROTECT     = 0x88
CMD_SET_ZERO_MODE   = 0x90   # power-on auto-return-to-zero; 0=disable (persists in MKS NVM)
CMD_SET_ZERO        = 0x91
CMD_SET_ACC         = 0xA4   # accel ramp; lower = smoother speed transitions on 0xF6
CMD_SET_MAX_TORQUE  = 0xA5   # MaxT 0..0x4B0 (default 0x4B0); only torque cap that works in CR_UART
CMD_CONSTANT_SPEED  = 0xF6   # continuous motion; [speed_dir]; gear=0 stops
CMD_ENABLE          = 0xF3
CMD_STOP            = 0xF7
CMD_MOVE_POS        = 0xFD   # two-stage response: status=1 starting -> status=2 complete (or 0 fail)

# ── Valve config ───────────────────────────────────────────────────────────
MICROSTEP       = 16         # 0x84 microstepping. (Reverted 64x->16x: the 64x homing trouble was
                              # a loose coupler, not microstep -- to be re-attempted after repair.)
                              # KEY: gear 1 (0xF6/0xFD) is ALWAYS 500 microsteps/s regardless of
                              # microstep, so more microsteps make that floor gear a SLOWER physical
                              # speed -> a slow breath without rounding to gear 0. Step-count consts
                              # below scale off _USTEP; recalibrate OPEN_STEPS after changing this.
_USTEP          = MICROSTEP // 16   # scale vs the original 16x baseline for microstep-count consts
OPEN_STEPS      = 2000 * _USTEP   # open extent in microsteps from the closed seat (motor_pos 0).
                                   # Placeholder ~useful range -- recalibrate via valve/calibrate.
MOVE_SPEED      = 4 * _USTEP  # 0xFD/0xF6 move gear; ~0.6 rev/s at any microstep. Lower physical
                              # speed = closed-loop doesn't demand peak current during motion.
CURRENT_GEAR    = 0x01       # 200 mA -- runtime SET_CURRENT doesn't apply, must bake in at boot
MAX_TORQUE      = 0x4B0      # 0xA5 ceiling, 0..0x4B0; only torque cap that works in CR_UART.
                              # Lower to cap contact force for gentle homing into the seat.

# 0xFD direction bit (OR'd with speed). Spec: bit7=0 CW, bit7=1 CCW.
# On THIS hardware (empirically verified 2026-06-03 during homing): bit7=1 drives
# the valve toward the OPEN stop, bit7=0 drives toward the closed seat. (The earlier
# comment had this backwards, which inverted all target moves.) HOME_DIR = 0x00 homes
# toward the closed seat = DIR_TOWARD_CLOSED.
DIR_TOWARD_OPEN   = 0x80
DIR_TOWARD_CLOSED = 0x00

# Homing direction = the literal 0xFD speed_dir bit. EMPIRICAL (2026-06-03):
# bit7=0 drives toward the CLOSED seat, bit7=1 toward the open stop. We home to the
# CLOSED seat (0x00): a controlled gentle inchworm touch gives a precise closed
# reference, safer than risking an operational slam. The finalize backs OFF toward
# open (HOME_DIR ^ 0x80) and zeros at that margin -- it never rests on/rams the seat.
HOME_DIR        = 0x00

# ── MQTT topics ────────────────────────────────────────────────────────────
TOPIC_VALVE_TARGET    = b"bush/fire/valve/target"
TOPIC_VALVE_HOME      = b"bush/fire/valve/home"
TOPIC_VALVE_STOP      = b"bush/fire/valve/stop"
TOPIC_VALVE_CALIBRATE = b"bush/fire/valve/calibrate"
TOPIC_VALVE_BREATH    = b"bush/fire/valve/breath"
TOPIC_VALVE_MAXTORQUE = b"bush/fire/valve/maxtorque"
TOPIC_VALVE_NUDGE     = b"bush/fire/valve/nudge"
TOPIC_VALVE_ACTUAL    = b"bush/fire/valve/actual"
TOPIC_VALVE_STATUS    = b"bush/fire/valve/status"
TOPIC_VALVE_ONLINE    = b"bush/fire/valve/online"

ALL_VALVE_TOPICS = [
    TOPIC_VALVE_TARGET,
    TOPIC_VALVE_HOME,
    TOPIC_VALVE_STOP,
    TOPIC_VALVE_CALIBRATE,
    TOPIC_VALVE_BREATH,
    TOPIC_VALVE_MAXTORQUE,
    TOPIC_VALVE_NUDGE,
]

# ── State ──────────────────────────────────────────────────────────────────
# states: "unknown", "homing", "idle", "moving", "stalled", "error"
state                = "unknown"
homed                = False
motor_pos_steps      = 0     # 0 = closed (seat margin), open_steps = open (post-homing ref).
                              # DERIVED from the 0x30 encoder, not dead-reckoned.
target_pos_steps     = 0
move_in_flight_delta = 0     # signed: +ve = toward open (direction/telemetry; fallback only)
_nudge_delta         = 0     # signed pulses of the in-flight debug nudge (fallback only)
open_steps           = OPEN_STEPS
last_error           = None

# Absolute position from the MT multi-turn encoder (0x30). Homing captures the
# zero raw and the sign; _encoder_pos_steps() maps a raw read to absolute steps.
ENC_PER_STEP         = 65536.0 / (200 * MICROSTEP)  # 20.48 counts/microstep at 16x (float)
_enc_zero_raw        = 0     # 0x30 raw at the homed backed-off margin (= motor_pos 0)
_enc_sign            = 1     # +1 if raw rises toward OPEN, -1 otherwise; set at homing
_pending_sync_target = 0     # step target awaiting the pre-move sync_read

pending_target  = None
last_target_ms  = 0
TARGET_MIN_MS   = 100        # don't issue moves faster than 10 Hz

_rx_buf         = bytearray()
_pending_cmd    = None
_cmd_sent_ms    = 0
CMD_TIMEOUT_MS  = 500
MOVE_TIMEOUT_MS = 8000       # full-travel @ gear 8 is ~5 s; 8 s catches stalls
                              # quickly so the de-energize safety net fires fast

# A 0xFD issued immediately after ENABLE doesn't execute on this MKS, so _issue_move
# energizes, stages the move here, and service() fires it after MOVE_SETTLE_MS.
_pending_move   = None       # staged 0xFD frame bytes, or None
_move_settle_at = 0
MOVE_SETTLE_MS  = 120        # post-ENABLE settle before the 0xFD (homing back-off uses ~100)

# Homing -- inchworm: discrete 0xFD steps toward HOME_DIR with an encoder read
# between each (reads are only reliable when the motor is stopped). Contact = a
# step that advances far less than the self-calibrated free-motion (cruise)
# delta. Gentle by construction (small slow steps); no torque cap works in CR_UART.
_home_started_ms   = 0
_home_seed_raw     = 0       # encoder raw before the first inch step; with contact_raw
                              # gives the OPEN/CLOSED encoder sign (_enc_sign)
_home_last_raw     = 0       # last encoder raw read (doubles as the per-step prev ref)
_home_last_delta   = 0       # last per-step |encoder delta| (telemetry)
_home_cruise       = 0       # free-motion baseline (mean of the first few free steps)
_home_inch_count   = 0
_home_contact_raw  = 0       # encoder raw at stop contact; home zero is set back here
HOME_TIMEOUT_MS    = 1800000 # 30 min. Measured full-open->seat home ~= 1119 inch steps
                              # (~4.2 rev) at ~1.1 s/step ~= 20 min; this covers it + margin.
HOME_INCH_STEPS    = 12 * _USTEP  # microsteps per inch step (~1.35 deg, fixed physical at any
                              # microstep). No torque cap works in CR_UART, so contact force is
                              # bounded only by how far the FOC tries to push past the stop =
                              # step size. Keep tiny so a contact never damages the closed seat.
HOME_INCH_SPEED    = 1       # 0xFD speed gear for the approach (slowest -> least current)
HOME_CONTACT_FRAC  = 0.6     # trip when a step advances < this fraction of the cruise
                              # baseline -- the ONSET of resistance, not a full encoder
                              # freeze. Drivetrain compliance lets the shaft keep rotating
                              # ~a full step into the stop while current ramps, so waiting
                              # for delta~0 rams hard; catch the first delta drop instead.
HOME_SETTLE_STEPS  = 2       # first steps after a (re)start ramp up slow -- skip them so
                              # they don't drag the cruise baseline down
HOME_CRUISE_STEPS  = 5       # free steps after settling, averaged into the cruise baseline
HOME_INCH_MAX      = 1500    # ~4.7 rev. Measured full-open->seat contact ~= 1119 inch steps
                              # (~4.2 rev); +34% margin. Raise if a home starts past full open.
HOME_BACKOFF_STEPS = 200 * _USTEP  # after contact, back off this far toward OPEN (fixed physical
                              # at any microstep) to VERIFY the motor moved freely (not jammed),
                              # then zero at that backed-off margin -- motor_pos 0 = a safe margin
                              # off the closed seat, never resting on / ramming it.
HOME_BACKOFF_MIN_FRAC = 0.5  # the back-off must move >= this fraction of its commanded
                              # distance (vs calibrated counts/step) or the motor is stuck

# Post-stall finalization (state="homing_finalize").
# After encoder-stall detection, we run a small fire-and-forget sequence:
# SET_ZERO -> SET_PROTECT(on) -> ENABLE, then drain stragglers and go idle.
_finalize_step    = 0
_finalize_next_ms = 0
FINALIZE_STEP_MS  = 100

# Publication
_last_status_ms = 0
_last_actual_ms = 0
STATUS_IDLE_MS  = 1000
STATUS_MOVE_MS  = 200
ACTUAL_MS       = 250

# Checksum-fail logging + CABLE CROSSTALK detector.
# Rate-limit fail logs so a degrading cable doesn't flood the USB CDC console.
# Track the last several outgoing opcode bytes; if a failed frame contains one
# of them, it's almost certainly our own TX bleeding into RX (the wire-level
# coupling we diagnosed at the bench on 2026-05-31 — see plans/uart-diagnostic).
_cksum_fail_count    = 0
_cksum_fail_xtalk    = 0
_cksum_fail_last_ms  = 0
_cksum_fail_last_head = None
_cksum_fail_last_cmd  = None
CKSUM_LOG_MS         = 2000

_recent_tx_bytes = []            # ring of recent outgoing opcode bytes (ints)
RECENT_TX_KEEP   = 16

# ── Breath oscillator ──────────────────────────────────────────────────────
# Skewed-sine modulation around target_pos_steps, driven by a smooth continuous 0xF6
# whose velocity is the sine derivative + a drift term restoring toward the baseline.
# Position is dead-reckoned between reads; the encoder (0x30) is read ONLY at the valley
# -- the bottom of the breath, where velocity rounds to a gear-0 rest and the motor is
# naturally stopped, so the read is reliable (this MKS won't answer 0x30 mid-motion).
# That re-grounds position once per cycle and bounds drift. Large baseline jumps
# (> BREATH_BIG_JUMP) interrupt with 0xF7 + 0xFD + auto-resume on move_done.
#
# Configurable at runtime via JSON to bush/fire/valve/breath.
_breath_enabled    = True
_breath_amplitude  = 0.04         # peak deviation from baseline, fraction of full travel
_breath_period_ms  = 5000
_breath_skew       = 0.5          # rise fraction of period; 0.5 = symmetric sine

_breath_phase_start_ms = 0
_breath_last_update_ms = 0
_breath_last_speed_dir = None     # last 0xF6 byte sent, or None if motor stopped
_breath_prev_t         = 0        # last tick's t_in_cycle (to detect the cycle-wrap = valley)
_breath_at_valley      = False    # halted at the bottom, waiting to take the stopped read
_pending_jump_target   = 0        # step target queued during exiting_breath
BREATH_UPDATE_MS       = 100      # encoder-read + 0xF6 cadence
MKS_SILENCE_LIMIT_MS   = 2000     # if no fresh encoder read for >this while breath is
                                  # driving, we've lost feedback -- cut + de-energize
_breath_last_good_read_ms = 0     # ticks_ms of last successful breath encoder read
BREATH_BIG_JUMP        = 0.10     # baseline change > this fraction triggers 0xFD jump
BREATH_ENTER_DEADBAND  = 5        # steps; within this of target, breathe without a move first
BREATH_ACC_VALUE       = 286      # 0xA4 SET_ACC; 286 = min documented = gentlest ramp
BREATH_MAX_GEAR        = min(127, 30 * _USTEP)  # safety clamp on speed_gear (same physical cap
                                  # at any microstep; 0x7F max)
BREATH_DRIFT_TAU_S     = 2.0      # breath drift-restore time constant (toward baseline)
STEPS_PER_GEAR_PER_SEC = 500      # at 16x microstep: gear * 9.375 RPM = gear * 500 steps/s


def _ticks_diff(later, earlier):
    return (later - earlier) & 0x3FFFFFFF


def _encoder_pos_steps(raw):
    """Absolute valve position in steps from a 0x30 raw reading. 0 = homed margin.
    Float scale (~20.48 cts/step); per-read rounding error < 1 step."""
    return int(round((raw - _enc_zero_raw) * _enc_sign / ENC_PER_STEP))


def _log_cksum_fail(head_bytes, cmd):
    global _cksum_fail_count, _cksum_fail_xtalk, _cksum_fail_last_ms
    global _cksum_fail_last_head, _cksum_fail_last_cmd
    _cksum_fail_count += 1
    _cksum_fail_last_head = head_bytes
    _cksum_fail_last_cmd = cmd
    # Crosstalk signature: middle byte of [E0 X Y] matches a recently sent
    # opcode. Genuine MKS responses put a 0/1/2 status byte there.
    if any(b in _recent_tx_bytes for b in head_bytes[1:]):
        _cksum_fail_xtalk += 1
    now = supervisor.ticks_ms()
    if _ticks_diff(now, _cksum_fail_last_ms) >= CKSUM_LOG_MS:
        if _cksum_fail_xtalk * 2 >= _cksum_fail_count:
            print(f"Valve: CABLE CROSSTALK suspected -- cksum fail x{_cksum_fail_count} "
                  f"({_cksum_fail_xtalk} match recent TX), last head={head_bytes}, cmd={cmd}. "
                  f"Spread UART data wires apart in the cable bundle.")
        else:
            print(f"Valve: cksum fail x{_cksum_fail_count} (last head={head_bytes}, cmd={cmd})")
        _cksum_fail_count = 0
        _cksum_fail_xtalk = 0
        _cksum_fail_last_ms = now


# ── Packet helpers ─────────────────────────────────────────────────────────

def _checksum(data):
    return sum(data) & 0xFF


def _send(cmd_bytes):
    pkt = bytes([MKS_ADDR]) + cmd_bytes
    pkt = pkt + bytes([_checksum(pkt)])
    uart.write(pkt)
    # Track the opcode byte for the crosstalk detector. Drop oldest if full.
    if cmd_bytes:
        if len(_recent_tx_bytes) >= RECENT_TX_KEEP:
            del _recent_tx_bytes[0]
        _recent_tx_bytes.append(cmd_bytes[0])


def _send_and_expect(cmd_bytes, label):
    global _pending_cmd, _cmd_sent_ms
    _pending_cmd = label
    _cmd_sent_ms = supervisor.ticks_ms()
    _send(cmd_bytes)


def _drain_uart_buffer():
    """Discard pending bytes from both hardware UART and software _rx_buf.
    Used at homing transitions to drop late ACKs from aborted commands."""
    global _rx_buf
    while True:
        avail = uart.in_waiting
        if avail <= 0:
            break
        uart.read(avail)
    _rx_buf = bytearray()


# ── Commands ───────────────────────────────────────────────────────────────

def cmd_stop():
    """Hard stop. If we were moving, mark not-homed since position is now unknown.
    Discards any queued target — the operator should re-issue after recovery."""
    global state, target_pos_steps, move_in_flight_delta, homed, pending_target
    global _breath_last_speed_dir, _pending_move
    # Send STOP and let its ACK arrive via the normal parser, then de-energize
    # (needle valve holds its own position; no reason to keep windings powered).
    _send_and_expect(bytes([CMD_STOP]), "stop")
    _send(bytes([CMD_ENABLE, 0x00]))
    _pending_move = None         # drop any move staged but not yet fired
    move_in_flight_delta = 0
    target_pos_steps = motor_pos_steps
    pending_target = None
    _breath_last_speed_dir = None
    if state in ("moving", "homing"):
        homed = False
        state = "unknown"
    elif state != "error":
        state = "idle"
    print("Valve: STOP")


def _issue_move(step_target):
    """Stage a relative 0xFD move toward absolute step_target: energize now, fire the
    0xFD after MOVE_SETTLE_MS (service()). Returns True if staged."""
    global target_pos_steps, move_in_flight_delta, state, _pending_move, _move_settle_at
    if not homed:
        print("Valve: refusing move -- not homed")
        return False
    if state != "idle" or _pending_cmd is not None:
        print(f"Valve: refusing move -- state={state} pending={_pending_cmd}")
        return False
    step_target = max(0, min(open_steps, step_target))
    delta = step_target - motor_pos_steps
    if delta == 0:
        target_pos_steps = step_target
        return False

    target_pos_steps = step_target
    move_in_flight_delta = delta
    direction = DIR_TOWARD_OPEN if delta > 0 else DIR_TOWARD_CLOSED   # +delta = higher pos = more open
    speed_dir = direction | (MOVE_SPEED & 0x7F)
    print(f"Valve: move {motor_pos_steps} -> {step_target} (d={delta} pulses)")
    # Energize now; stage the 0xFD and let service() fire it after MOVE_SETTLE_MS -- a
    # 0xFD right after ENABLE doesn't execute on this MKS (the move never completes).
    _pending_move = bytes([CMD_MOVE_POS, speed_dir]) + abs(delta).to_bytes(4, "big")
    _send(bytes([CMD_ENABLE, 0x01]))
    _move_settle_at = (supervisor.ticks_ms() + MOVE_SETTLE_MS) & 0x3FFFFFFF
    state = "moving"        # guard other branches while the staged move settles
    return True


# ── Encoder-grounded move/nudge finalization ───────────────────────────────
# motor_pos_steps is set from a 0x30 read after every move/nudge, not by += delta.

def _dispatch_pending_target():
    """Issue the move (or enter breathing if already at target) for the step target
    stashed by the idle branch. Called once a sync_read has grounded motor_pos_steps."""
    global target_pos_steps
    step_target = _pending_sync_target
    if _breath_enabled and homed and abs(step_target - motor_pos_steps) <= BREATH_ENTER_DEADBAND:
        target_pos_steps = step_target
        _enter_breathing(supervisor.ticks_ms())
    else:
        _issue_move(step_target)


def _on_sync_read(raw):
    """Pre-move sync_read landed: ground motor_pos_steps, then dispatch the move."""
    global motor_pos_steps
    motor_pos_steps = max(0, min(open_steps, _encoder_pos_steps(raw)))
    _dispatch_pending_target()


def _finalize_move_to(pos):
    """Land a completed move at absolute step `pos`: auto-enter breathing (keeps the
    motor energized) if enabled, else de-energize and idle."""
    global motor_pos_steps, move_in_flight_delta, state
    motor_pos_steps = max(0, min(open_steps, pos))
    move_in_flight_delta = 0
    print(f"Valve: move complete, pos={motor_pos_steps}")
    if _breath_enabled and homed:
        _enter_breathing(supervisor.ticks_ms())
    else:
        _send(bytes([CMD_STOP]))           # clear the move hold so ENABLE 0 releases
        _send(bytes([CMD_ENABLE, 0x00]))   # needle valve holds its own position
        state = "idle"


def _on_move_sync(raw):
    _finalize_move_to(_encoder_pos_steps(raw))


def _finalize_nudge_to(pos):
    """Land a completed nudge at `pos` (NO soft-limit clamp -- nudge is the unclamped
    characterization path), then de-energize and idle."""
    global motor_pos_steps, _nudge_delta, state
    motor_pos_steps = pos
    _nudge_delta = 0
    _send(bytes([CMD_STOP]))           # clear the move hold so ENABLE 0 releases
    _send(bytes([CMD_ENABLE, 0x00]))   # de-energize: needle valve holds position
    state = "idle"
    print(f"Valve: nudge done pos~={motor_pos_steps} -- de-energized")


def _on_nudge_sync(raw):
    _finalize_nudge_to(_encoder_pos_steps(raw))


# ── Homing chain ──────────────────────────────────────────────────────────
# Every command in the homing sequence is sent one at a time via
# _send_and_expect; each ACK in _parse_response chains to the next step.
# This avoids ACK alignment problems from batched bare-_send setup commands.

def cmd_home():
    """Begin homing sequence. Asynchronous: completes via the response chain
    and the encoder-stall fallback in service()."""
    global state, homed, _home_started_ms, pending_target
    global _home_last_raw, _home_last_delta, _home_cruise, _home_inch_count, _home_contact_raw
    global _breath_last_speed_dir, _pending_move

    # If breathing was active, stop the 0xF6 motion first so the inchworm steps
    # aren't fighting a continuous-speed command.
    if state == "breathing" or _breath_last_speed_dir is not None:
        _send(bytes([CMD_STOP]))
        _breath_last_speed_dir = None

    # If anything is in flight, send STOP and start fresh once it ACKs.
    # cmd_home itself starts the chain via "home_clear_protect" below;
    # we leave any pending response to be discarded by parser realignment.
    # Discard any queued target — homing recalibrates the reference position,
    # so a target queued under the old reference is no longer meaningful.
    pending_target = None
    homed = False
    state = "homing"
    _drain_uart_buffer()
    _home_started_ms = supervisor.ticks_ms()
    _home_last_raw = 0
    _home_last_delta = 0
    _home_cruise = 0
    _home_inch_count = 0
    _home_contact_raw = 0
    _pending_move = None

    print("Valve: homing -- chain start (clear latched protect)")
    _send_and_expect(bytes([CMD_CLEAR_PROTECT]), "home_clear_protect")


def _complete_homing_by_stall():
    """Contact with the stop. De-energize, then finalize (in service()) backs off
    HOME_BACKOFF_STEPS to verify the motor moves freely (not jammed), returns to this
    contact point, and sets zero there -- 0 = the actual hard stop."""
    global state, homed, motor_pos_steps, target_pos_steps, move_in_flight_delta
    global _pending_cmd, _finalize_step, _finalize_next_ms, _home_contact_raw
    global _enc_sign
    _home_contact_raw = _home_last_raw       # remember the stop so we can return to it
    # Inching ran toward the CLOSED seat: raw moved seed->contact while closing, so raw
    # rises toward OPEN iff contact < seed. Captured here so every homing path sets it.
    _enc_sign = 1 if (_home_seed_raw - _home_last_raw) > 0 else -1
    print(f"Valve: home contact at raw={_home_last_raw} (enc_sign={_enc_sign}) -- finalize")
    homed = True
    motor_pos_steps = 0
    target_pos_steps = 0
    move_in_flight_delta = 0
    state = "homing_finalize"
    _pending_cmd = None
    _finalize_step = 0
    _send(bytes([CMD_STOP]))
    # Drop windings immediately so we don't sit at full current against the stop.
    # Finalize re-enables, backs off, then sets zero at the backed-off position.
    _send(bytes([CMD_ENABLE, 0x00]))
    _finalize_next_ms = (supervisor.ticks_ms() + FINALIZE_STEP_MS) & 0x3FFFFFFF


def _service_finalize(now):
    """Advance the post-stall finalize state machine. Called from service().
    Steps 0-2 are fire-and-forget (ACKs dropped as stragglers); step 2 issues the
    back-off 0xFD and hands off to the home_backoff_* ACK chain in _parse_response."""
    global _finalize_step, _finalize_next_ms
    if _finalize_step >= 3:
        return  # back-off move issued; waiting on its ACK chain (_finish_homing)
    if _ticks_diff(now, _finalize_next_ms) >= 0x1FFFFFFF:
        return  # not time yet
    step = _finalize_step
    if step == 0:
        _send(bytes([CMD_SET_PROTECT, 0x00]))   # keep stall-protect off (matches normal ops)
        _finalize_step = 1
        _finalize_next_ms = (now + FINALIZE_STEP_MS) & 0x3FFFFFFF
    elif step == 1:
        _send(bytes([CMD_ENABLE, 0x01]))        # re-energize for the back-off move
        _finalize_step = 2
        _finalize_next_ms = (now + FINALIZE_STEP_MS) & 0x3FFFFFFF
    elif step == 2:
        # Back off the open stop (opposite of the homing direction bit) to verify the
        # motor moves freely; _home_verify_backoff then returns to the contact and zeros.
        speed_dir = (HOME_DIR ^ 0x80) | (MOVE_SPEED & 0x7F)
        pulse_bytes = HOME_BACKOFF_STEPS.to_bytes(4, "big")
        print(f"Valve: homing -- backing off {HOME_BACKOFF_STEPS} steps from stop")
        _send_and_expect(bytes([CMD_MOVE_POS, speed_dir]) + pulse_bytes, "home_backoff_start")
        _finalize_step = 3


def _home_verify_backoff(raw):
    """Back-off complete: confirm the motor actually moved away from the stop (0xFD
    reports complete even when jammed). If it moved freely, drive back to the contact
    point so we can zero there; if it barely moved, the motor is stuck -> error."""
    global state, last_error
    moved = abs(raw - _home_contact_raw)
    expected = HOME_BACKOFF_STEPS * _home_cruise // HOME_INCH_STEPS if _home_cruise > 0 else 0
    if expected and moved < expected * HOME_BACKOFF_MIN_FRAC:
        _send(bytes([CMD_ENABLE, 0x00]))
        state = "error"
        last_error = "home_stuck"
        print(f"Valve: backoff moved only {moved} (expected ~{expected}) -- STUCK at stop, disabled")
        return
    # Do NOT drive back into the stop: toward the force-sensitive closed seat a blind
    # relative move would ram it. Zero at this backed-off margin instead.
    print(f"Valve: backoff verified ({moved} cts, expected ~{expected}) -- not stuck")
    _finish_homing()


def _finish_homing():
    """Back-off (away from the seat) verified. Set THIS backed-off position as zero --
    a safe margin off the closed seat, so motor_pos 0 never rests on the force-sensitive
    seat. De-energize, then re-read the encoder to seed the absolute zero."""
    global motor_pos_steps, target_pos_steps, move_in_flight_delta
    _send(bytes([CMD_STOP]))             # clear the backoff move's hold first, else ENABLE 0
                                          # won't release the servo (it keeps holding)
    _send(bytes([CMD_SET_ZERO, 0x00]))   # 0x91: set this backed-off position as zero
    motor_pos_steps = 0
    target_pos_steps = 0
    move_in_flight_delta = 0
    _send(bytes([CMD_ENABLE, 0x00]))     # de-energize at idle: needle valve holds position
    _drain_uart_buffer()
    # Seed _enc_zero_raw from a fresh read AFTER SET_ZERO -- correct whether or not 0x91
    # resets the 0x30 readout. _on_home_zero_seed lands us in "idle".
    _send_and_expect(bytes([CMD_READ_ENCODER]), "home_zero_seed")
    print("Valve: homed (zero = margin off closed seat, de-energized) -- seeding enc zero")


def _on_home_zero_seed(raw):
    """home_zero_seed read landed: the post-SET_ZERO raw IS motor_pos 0."""
    global _enc_zero_raw, state
    _enc_zero_raw = raw
    state = "idle"
    print(f"Valve: enc zero seeded raw={raw} sign={_enc_sign} -- ready for moves")


# ── Inchworm homing step ───────────────────────────────────────────────────

def _home_issue_inch():
    """Issue one tiny 0xFD step toward the open stop. The motor stays enabled (de-energizing
    between steps stalls the next move on this MKS), but after each step a STOP (0xF7) clears
    the position-loop windup so the motor holds at its ACTUAL position instead of grinding
    toward the unreachable target at high current (no torque cap works in CR_UART).
    HOME_DIR is the literal 0xFD direction bit (toward open here)."""
    speed_dir = HOME_DIR | (HOME_INCH_SPEED & 0x7F)
    _send_and_expect(bytes([CMD_MOVE_POS, speed_dir]) + HOME_INCH_STEPS.to_bytes(4, "big"),
                     "home_inch_move")


def _home_inch_step(raw):
    """One inch step's encoder read. Contact = ONSET of resistance, not a full encoder
    freeze: drivetrain compliance lets the shaft keep rotating ~a full step into the stop
    while current ramps, so waiting for delta~0 rams hard. Freeze a robust cruise baseline
    (mean of the first HOME_CRUISE_STEPS free steps, skipping step-1 settling), then trip the
    first step that falls below HOME_CONTACT_FRAC of it. HOME_INCH_MAX bounds the run."""
    global _home_last_raw, _home_last_delta, _home_cruise, _home_inch_count
    global state, last_error
    delta = abs(raw - _home_last_raw)
    _home_last_raw = raw
    _home_last_delta = delta
    _home_inch_count += 1
    n = _home_inch_count
    cal_end = HOME_SETTLE_STEPS + HOME_CRUISE_STEPS
    contact = False
    if HOME_SETTLE_STEPS < n <= cal_end:
        _home_cruise += delta                                  # accumulate baseline sum
        if n == cal_end:
            _home_cruise = _home_cruise // HOME_CRUISE_STEPS    # freeze as the mean
        print(f"Valve: inch {n} raw={raw} d={delta} (calibrating cruise)")
    elif n > cal_end and _home_cruise > 0:
        contact = delta < _home_cruise * HOME_CONTACT_FRAC
        print(f"Valve: inch {n} raw={raw} d={delta} cruise={_home_cruise}")
    else:
        print(f"Valve: inch {n} raw={raw} d={delta} (settling)")
    if contact:
        _complete_homing_by_stall()
    elif n >= HOME_INCH_MAX:
        cmd_stop()
        _send(bytes([CMD_ENABLE, 0x00]))
        state = "error"
        last_error = "home_no_contact"
        print("Valve: inchworm hit HOME_INCH_MAX with no contact -- motor disabled")
    else:
        _home_issue_inch()


# ── Response parsing ───────────────────────────────────────────────────────

def _parse_response():
    global _rx_buf, _pending_cmd, _cmd_sent_ms
    global state, homed, last_error, motor_pos_steps, move_in_flight_delta
    global _home_last_raw, _home_last_delta, _home_seed_raw
    global _pending_jump_target, _nudge_delta
    global _breath_enabled, _breath_last_speed_dir

    if len(_rx_buf) < 3:
        return False
    if _rx_buf[0] != MKS_ADDR:
        _rx_buf = _rx_buf[1:]
        return True

    cmd = _pending_cmd

    if cmd in ("home_inch_seed", "home_inch_read", "home_verify", "home_zero_seed",
               "sync_read", "move_sync", "nudge_sync", "breath_read"):
        if len(_rx_buf) < 8:
            return False
        if _checksum(_rx_buf[0:7]) != _rx_buf[7]:
            # Strip 1 byte and let the alignment loop find the next 0xE0,
            # rather than dropping a full 8 bytes of potentially-real data.
            _log_cksum_fail(list(_rx_buf[0:8]), cmd)
            _rx_buf = _rx_buf[1:]
            return True
        carry = struct.unpack(">i", bytes(_rx_buf[1:5]))[0]
        value = struct.unpack(">H", bytes(_rx_buf[5:7]))[0]
        raw = (carry << 16) | value
        _rx_buf = _rx_buf[8:]
        _pending_cmd = None            # cleared first; the handlers re-arm if they chain a send
        if cmd == "home_inch_seed":
            _home_seed_raw = raw       # absolute baseline for the enc-sign capture
            _home_last_raw = raw       # baseline before the first step
            _home_last_delta = 0
            _home_issue_inch()
        elif cmd == "home_inch_read":
            _home_inch_step(raw)
        elif cmd == "home_verify":
            _home_verify_backoff(raw)
        elif cmd == "home_zero_seed":
            _on_home_zero_seed(raw)
        elif cmd == "sync_read":
            _on_sync_read(raw)
        elif cmd == "move_sync":
            _on_move_sync(raw)
        elif cmd == "nudge_sync":
            _on_nudge_sync(raw)
        else:                          # breath_read
            _on_breath_read(raw)
        return True

    # All other responses: addr + status + crc = 3 bytes
    if _checksum(_rx_buf[0:2]) != _rx_buf[2]:
        # Strip 1 byte to allow re-alignment to the next 0xE0.
        _log_cksum_fail(list(_rx_buf[0:3]), cmd)
        _rx_buf = _rx_buf[1:]
        return True
    status = _rx_buf[1]
    _rx_buf = _rx_buf[3:]

    if cmd == "move_start":
        if status == 1:
            state = "moving"
            _pending_cmd = "move_done"
            _cmd_sent_ms = supervisor.ticks_ms()
        elif status == 0:
            _pending_cmd = None
            state = "error"
            last_error = "move_rejected"
            move_in_flight_delta = 0
            print("Valve: 0xFD start status=0 (rejected)")
        else:
            _pending_cmd = None
            state = "error"
            last_error = "move_bad_start"
            move_in_flight_delta = 0
            print(f"Valve: 0xFD start unexpected status={status}")
        return True

    if cmd == "move_done":
        if status == 2:
            # Ground the new position on the absolute encoder (motor is stopped at
            # target now) instead of dead-reckoning; _on_move_sync finalizes. Keep
            # move_in_flight_delta for the timeout fallback in _check_timeout.
            _send_and_expect(bytes([CMD_READ_ENCODER]), "move_sync")
        elif status == 0:
            _pending_cmd = None
            state = "stalled"
            last_error = "move_stalled"
            move_in_flight_delta = 0
            _breath_enabled = False
            _breath_last_speed_dir = None
            # Stall-protect halted motion but left windings energized at
            # full SET_CURRENT. STOP clears the hold, then de-energize.
            _send(bytes([CMD_STOP]))
            _send(bytes([CMD_ENABLE, 0x00]))
            print("Valve: 0xFD complete status=0 -- motor stalled, motor disabled")
        elif status == 1:
            # MKS internally buffers ACKs and occasionally emits a stale
            # [E0 01 E1] (byte-identical to a 0xFD status=1) during the move.
            # Cable crosstalk was fixed by separating wires; this is a
            # different, MKS-firmware-level symptom. Real status=2 still comes.
            print("Valve: ignoring stray status=1 during move_done")
        else:
            _pending_cmd = None
            state = "error"
            last_error = "move_bad_done"
            move_in_flight_delta = 0
            print(f"Valve: 0xFD complete unexpected status={status}")
        return True

    if cmd == "home_backoff_start":
        if status == 1:
            _pending_cmd = "home_backoff_done"
            _cmd_sent_ms = supervisor.ticks_ms()
        else:
            # Back-off move rejected -- finish home where we are rather than retry.
            _pending_cmd = None
            print(f"Valve: backoff start status={status} -- finishing home in place")
            _finish_homing()
        return True

    if cmd == "home_backoff_done":
        if status == 2:
            # Reported complete -- read the encoder to verify it actually moved (0xFD
            # reports complete even when jammed), then return to the contact and zero.
            _pending_cmd = None
            _send_and_expect(bytes([CMD_READ_ENCODER]), "home_verify")
        elif status == 1:
            print("Valve: ignoring stray status=1 during backoff")
        elif status == 0:
            # Backing off moves AWAY from the stop, so a stall here is unexpected.
            _pending_cmd = None
            _send(bytes([CMD_ENABLE, 0x00]))
            state = "error"
            last_error = "home_backoff_stalled"
            print("Valve: backoff stalled?! motor disabled")
        else:
            _pending_cmd = None
            _send_and_expect(bytes([CMD_READ_ENCODER]), "home_verify")
        return True

    if cmd == "nudge_start":
        if status == 1:
            _pending_cmd = "nudge_done"
            _cmd_sent_ms = supervisor.ticks_ms()
        else:
            _pending_cmd = None
            state = "idle"
            print(f"Valve: nudge start status={status}")
        return True

    if cmd == "nudge_done":
        if status == 1:
            print("Valve: ignoring stray status=1 during nudge")
        elif status == 0:
            _pending_cmd = None
            _send(bytes([CMD_ENABLE, 0x00]))
            state = "idle"
            print("Valve: nudge stalled (status=0) -- hit a stop, de-energized")
        else:
            # Ground on the encoder; _on_nudge_sync de-energizes and idles.
            _send_and_expect(bytes([CMD_READ_ENCODER]), "nudge_sync")
        return True

    if cmd == "breath":
        # 0xF6 ACK during breathing: status may be 0 or 1 -- we don't escalate, the next
        # tick reissues a fresh 0xF6 anyway.
        _pending_cmd = None
        return True

    if cmd == "breath_stop":
        # STOP ACK during big-jump exit. Transition to idle and issue the queued 0xFD.
        _pending_cmd = None
        state = "idle"
        target = _pending_jump_target
        _pending_jump_target = 0
        _issue_move(target)
        return True

    if cmd == "breath_stop_idle":
        # User disabled breathing. STOP ACK -> de-energize and idle.
        _pending_cmd = None
        _send(bytes([CMD_ENABLE, 0x00]))
        state = "idle"
        return True

    # ── Homing chain -> inchworm ───────────────────────────────────────
    if cmd == "home_clear_protect":
        _pending_cmd = None
        _send_and_expect(bytes([CMD_SET_PROTECT, 0x00]), "home_protect_off")
        return True

    if cmd == "home_protect_off":
        # Guarantee the motor is energized for the 0xFD inch steps.
        _pending_cmd = None
        _send_and_expect(bytes([CMD_ENABLE, 0x01]), "home_enable")
        return True

    if cmd == "home_enable":
        _pending_cmd = None
        print(f"Valve: homing -- inchworm toward stop (dir=0x{HOME_DIR:02X})")
        _send_and_expect(bytes([CMD_READ_ENCODER]), "home_inch_seed")
        return True

    if cmd == "home_inch_move":
        # 0xFD ACK: status=1 start (informational), 2 complete, 0 stall. A blocked 0xFD can
        # return status=2 with NO preceding status=1, so ignore status=1. On completion/stall,
        # STOP (0xF7) to clear the position-loop windup: the motor then holds at its ACTUAL
        # position (no error -> low current) instead of grinding toward the unreachable target.
        # Motor stays enabled so the next step still moves. Contact judged by encoder, not status.
        if status == 1:
            _cmd_sent_ms = supervisor.ticks_ms()   # move started; restart the completion clock
        else:
            _send_and_expect(bytes([CMD_STOP]), "home_inch_brake")
        return True

    if cmd == "home_inch_brake":
        _pending_cmd = None
        _send_and_expect(bytes([CMD_READ_ENCODER]), "home_inch_read")
        return True

    if cmd == "stop":
        _pending_cmd = None
        return True

    # Unmatched / stray ACK (e.g. post-contact finalize sends, or late ACKs
    # from STOP after an abort). Silently drop.
    return True


def _drain_uart_into_buf():
    global _rx_buf
    data = uart.read(64)
    if data:
        _rx_buf.extend(data)


def _check_mks_silence(now):
    """Backstop: the breath re-grounds on the encoder once per cycle at the valley, so if no
    valley read has landed for a few breath periods while breathing, we've lost the MKS
    (dead/unresponsive UART) -- de-energize rather than drive blind. (The controller itself
    isn't hung: a home works after.)"""
    global state, last_error, _breath_enabled, _breath_last_speed_dir
    if state != "breathing":
        return
    if _breath_last_good_read_ms == 0:
        return
    # Sample fresh: _breath_last_good_read_ms can be set during this loop's parse phase
    # (e.g. _enter_breathing), i.e. AFTER the caller's `now` -- using the stale `now` makes
    # _ticks_diff wrap to a huge value and false-trips the cut.
    now = supervisor.ticks_ms()
    limit = MKS_SILENCE_LIMIT_MS
    if 3 * _breath_period_ms > limit:        # valley reads land ~once per breath period
        limit = 3 * _breath_period_ms
    if _ticks_diff(now, _breath_last_good_read_ms) < limit:
        return
    _send(bytes([CMD_CONSTANT_SPEED, 0x00]))
    _send(bytes([CMD_STOP]))
    _send(bytes([CMD_ENABLE, 0x00]))
    _breath_enabled = False
    _breath_last_speed_dir = None
    state = "stalled"
    last_error = "mks_silent"
    print("Valve: no valley encoder read in 3 breaths -- lost MKS, motor disabled")


def _check_timeout():
    global _pending_cmd, state, last_error, _breath_enabled, _breath_last_speed_dir
    if _pending_cmd is None:
        return
    timeout = MOVE_TIMEOUT_MS if _pending_cmd in ("move_done", "home_backoff_done", "nudge_done", "home_inch_move") else CMD_TIMEOUT_MS
    if _ticks_diff(supervisor.ticks_ms(), _cmd_sent_ms) < timeout:
        return
    print(f"Valve: UART timeout waiting for {_pending_cmd}")
    # Breath read/move are best-effort: on a dropped frame, abandon without escalating;
    # the next breath tick re-reads the encoder and reissues. (Bounded by the silence
    # backstop if reads genuinely stop.)
    if _pending_cmd in ("breath", "breath_read"):
        _pending_cmd = None
        return
    if _pending_cmd == "sync_read":
        _pending_cmd = None
        _dispatch_pending_target()              # dispatch against the still-fresh motor_pos_steps
        return
    if _pending_cmd == "move_sync":
        _pending_cmd = None
        _finalize_move_to(motor_pos_steps + move_in_flight_delta)   # one-time dead-reckon fallback
        return
    if _pending_cmd == "nudge_sync":
        _pending_cmd = None
        _finalize_nudge_to(motor_pos_steps + _nudge_delta)
        return
    if _pending_cmd == "home_zero_seed":
        # We MUST land a post-SET_ZERO read to have a valid absolute reference -- idling on
        # a stale zero could drive a wrong absolute target into the force-sensitive seat.
        # The motor is de-energized and stationary at the margin, so just re-issue (like
        # the inch-read retry). Stays in homing_finalize -> no moves accepted meanwhile.
        print("Valve: re-reading encoder after home_zero_seed timeout")
        _send_and_expect(bytes([CMD_READ_ENCODER]), "home_zero_seed")
        return
    # A dropped encoder frame during homing shouldn't kill the home -- the motor is
    # stopped between inch steps, so just re-issue the read. HOME_TIMEOUT_MS is the
    # real backstop for a genuinely unresponsive MKS.
    if _pending_cmd in ("home_inch_seed", "home_inch_read"):
        label = _pending_cmd
        print(f"Valve: re-reading encoder after {label} timeout")
        _send_and_expect(bytes([CMD_READ_ENCODER]), label)
        return
    if _pending_cmd == "home_inch_brake":
        # STOP ACK dropped (or STOP-when-stopped didn't ACK) -- the STOP already went
        # out, so just advance to the encoder read instead of aborting the home.
        print("Valve: home_inch_brake ACK missed -- advancing to encoder read")
        _send_and_expect(bytes([CMD_READ_ENCODER]), "home_inch_read")
        return
    if _pending_cmd == "home_inch_move":
        # No move ACK in 8s -> the 0xFD was almost certainly not received (lost TX),
        # so re-issue it. (Bounded by HOME_TIMEOUT_MS.) The inchworm thus survives any
        # single dropped frame on this flaky UART instead of aborting the whole home.
        print("Valve: re-issuing inch move after home_inch_move timeout")
        _home_issue_inch()
        return
    # Any other timeout where motor could be energized: cut motion + de-energize
    # so we don't cook at SET_CURRENT (1.5-1.9A in stall). STOP clears a 0xFD hold
    # too (ENABLE 0 alone won't release an active servo hold).
    _send(bytes([CMD_CONSTANT_SPEED, 0x00]))
    _send(bytes([CMD_STOP]))
    _send(bytes([CMD_ENABLE, 0x00]))
    _breath_enabled = False
    _breath_last_speed_dir = None
    print(f"Valve: motor disabled after {_pending_cmd} timeout")
    last_error = f"timeout_{_pending_cmd}"
    _pending_cmd = None
    if state != "stalled":
        state = "error"


# ── MQTT inbound ───────────────────────────────────────────────────────────

def handle_mqtt(topic, payload):
    global pending_target, open_steps
    if topic == TOPIC_VALVE_TARGET:
        val = _parse_float(payload)
        if val is None:
            print(f"Valve: bad target payload: {payload}")
            return
        pending_target = max(0.0, min(1.0, val))
    elif topic == TOPIC_VALVE_HOME:
        cmd_home()
    elif topic == TOPIC_VALVE_STOP:
        cmd_stop()
    elif topic == TOPIC_VALVE_CALIBRATE:
        steps = _parse_int(payload)
        if steps is None or not (100 <= steps <= 100000):
            print(f"Valve: bad calibrate payload: {payload}")
            return
        open_steps = steps
        print(f"Valve: open_steps = {open_steps}")
    elif topic == TOPIC_VALVE_BREATH:
        _handle_breath_payload(payload)
    elif topic == TOPIC_VALVE_MAXTORQUE:
        t = _parse_int(payload)
        if t is None or not (0 <= t <= 0x4B0):
            print(f"Valve: bad maxtorque payload: {payload}")
            return
        _set_max_torque(t)
    elif topic == TOPIC_VALVE_NUDGE:
        _cmd_nudge(_parse_int(payload))


def _handle_breath_payload(payload):
    """Partial update of breath params. JSON keys: amplitude, period_ms, skew,
    enabled. Omitted fields stay at current value."""
    global _breath_enabled, _breath_amplitude, _breath_period_ms, _breath_skew
    if isinstance(payload, (bytes, bytearray)):
        try:
            payload = payload.decode("utf-8")
        except UnicodeError:
            return
    try:
        data = json.loads(payload)
    except (ValueError, TypeError):
        print(f"Valve: bad breath payload: {payload}")
        return
    if not isinstance(data, dict):
        return
    if "amplitude" in data:
        try:
            _breath_amplitude = max(0.0, min(0.5, float(data["amplitude"])))
        except (ValueError, TypeError):
            pass
    if "period_ms" in data:
        try:
            _breath_period_ms = max(100, min(60000, int(data["period_ms"])))
        except (ValueError, TypeError):
            pass
    if "skew" in data:
        try:
            _breath_skew = max(0.05, min(0.95, float(data["skew"])))
        except (ValueError, TypeError):
            pass
    if "enabled" in data:
        was_enabled = _breath_enabled
        _breath_enabled = bool(data["enabled"])
        if was_enabled and not _breath_enabled and state == "breathing":
            _exit_breath_to_idle()
    print(f"Valve: breath A={_breath_amplitude:.3f} T={_breath_period_ms}ms skew={_breath_skew:.2f} en={_breath_enabled}")


def _parse_float(payload):
    if isinstance(payload, (bytes, bytearray)):
        try:
            payload = payload.decode("utf-8")
        except UnicodeError:
            return None
    try:
        return float(payload)
    except (ValueError, TypeError):
        pass
    try:
        data = json.loads(payload)
        if isinstance(data, dict):
            v = data.get("target", data.get("value"))
            return float(v) if v is not None else None
        return float(data)
    except (ValueError, TypeError, KeyError, AttributeError):
        return None


def _parse_int(payload):
    if isinstance(payload, (bytes, bytearray)):
        try:
            payload = payload.decode("utf-8")
        except UnicodeError:
            return None
    try:
        return int(payload)
    except (ValueError, TypeError):
        pass
    try:
        data = json.loads(payload)
        if isinstance(data, dict):
            v = data.get("steps", data.get("value"))
            return int(v) if v is not None else None
        return int(data)
    except (ValueError, TypeError, KeyError, AttributeError):
        return None


# ── Init ───────────────────────────────────────────────────────────────────
# Init is blocking on purpose: each setup command's ACK is verified before
# moving on, so a corrupted SET_MODE ACK can't let the parser drift through
# garbage and silently "succeed" with CR_UART never actually set.


def _blocking_drain():
    global _rx_buf
    while True:
        avail = uart.in_waiting
        if avail <= 0:
            break
        uart.read(avail)
    _rx_buf = bytearray()


def _blocking_wait_ack(timeout_ms, accept_statuses):
    """Read UART until we see a valid 3-byte [E0, status, crc] where status is
    in accept_statuses. Returns the status, or None on timeout. Same 1-byte
    re-alignment strategy the async parser uses, so it tolerates TX echo and
    bit-flipped echo bytes."""
    deadline = (supervisor.ticks_ms() + timeout_ms) & 0x3FFFFFFF
    buf = bytearray()
    while True:
        if _ticks_diff(supervisor.ticks_ms(), deadline) < 0x1FFFFFFF:
            return None
        avail = uart.in_waiting
        if avail:
            data = uart.read(avail)
            if data:
                buf.extend(data)
        while len(buf) >= 3:
            if buf[0] != MKS_ADDR:
                buf = buf[1:]
                continue
            if _checksum(buf[0:2]) != buf[2]:
                buf = buf[1:]
                continue
            status = buf[1]
            buf = buf[3:]
            if status in accept_statuses:
                return status
        time.sleep(0.005)


def _blocking_setup(cmd_bytes, timeout_ms=500):
    """Send a setup command (SET_MODE etc.) and wait for status=1."""
    _blocking_drain()
    pkt = bytes([MKS_ADDR]) + cmd_bytes
    pkt = pkt + bytes([_checksum(pkt)])
    uart.write(pkt)
    return _blocking_wait_ack(timeout_ms, (0, 1))


def _set_max_torque(value):
    """0xA5 SET_MAX_TORQUE, 0..0x4B0. Only torque/current ceiling that works in
    CR_UART; lower = gentler contact force at a stop. Bare send (ACK unverified)."""
    value = max(0, min(0x4B0, int(value)))
    _send(bytes([CMD_SET_MAX_TORQUE, (value >> 8) & 0xFF, value & 0xFF]))
    print(f"Valve: SET_MAX_TORQUE {value} (0x{value:03X})")


def _cmd_nudge(deg):
    """Debug/characterization: raw relative move of `deg` degrees with NO soft-limit
    clamp. +deg = toward closed (the seat), -deg = toward open. For feeling out the
    closed seat in small steps at a capped MaxT: nudge, read the supply current while it
    holds, then nudge again or back off. De-energizes if the move reports a stall."""
    global state, _nudge_delta
    if deg is None:
        print("Valve: bad nudge payload")
        return
    if state not in ("idle", "unknown") or _pending_cmd is not None:
        print(f"Valve: refusing nudge -- state={state} pending={_pending_cmd}")
        return
    deg = max(-360, min(360, deg))
    steps = abs(deg) * (200 * MICROSTEP) // 360   # microsteps per rev = 200 full-steps * microstep
    if steps == 0:
        return
    direction = DIR_TOWARD_CLOSED if deg > 0 else DIR_TOWARD_OPEN
    speed_dir = direction | (MOVE_SPEED & 0x7F)
    _nudge_delta = steps if deg > 0 else -steps
    state = "nudging"
    print(f"Valve: nudge {deg} deg ({'close' if deg > 0 else 'open'} {steps} steps)")
    _send(bytes([CMD_ENABLE, 0x01]))   # energize for the nudge (idle is de-energized)
    _send_and_expect(bytes([CMD_MOVE_POS, speed_dir]) + steps.to_bytes(4, "big"), "nudge_start")


def init():
    """Configure MKS, enable motor. Blocking; runs once at boot. Motion
    verification happens later via the homing encoder-stall path -- a fresh
    boot can land against a hard stop, where any verify-by-motion is unsafe."""
    global state, last_error
    print("Valve: init UART GP4/GP5 @ 115200")
    time.sleep(0.2)
    _blocking_drain()
    acc_hi = (BREATH_ACC_VALUE >> 8) & 0xFF
    acc_lo = BREATH_ACC_VALUE & 0xFF
    for attempt in range(2):
        ok = (_blocking_setup(bytes([CMD_SET_MODE, 0x02])) == 1
              and _blocking_setup(bytes([CMD_SET_ZERO_MODE, 0x00])) == 1   # kill the persisted
                                                                            # power-on auto-home ram
              and _blocking_setup(bytes([CMD_SET_MICROSTEP, MICROSTEP])) == 1
              and _blocking_setup(bytes([CMD_SET_CURRENT, CURRENT_GEAR])) == 1
              and _blocking_setup(bytes([CMD_SET_PROTECT, 0x00])) == 1
              and _blocking_setup(bytes([CMD_SET_ACC, acc_hi, acc_lo])) == 1
              and _blocking_setup(bytes([CMD_ENABLE, 0x01])) == 1)
        if ok:
            _set_max_torque(MAX_TORQUE)   # bare send after the blocking chain
            _send(bytes([CMD_STOP]))           # clear any inherited position hold -- a Pico
                                                # reload does NOT reset the MKS, and ENABLE 0
                                                # alone won't release an active servo hold
            _send(bytes([CMD_ENABLE, 0x00]))   # de-energize at idle: the needle valve holds
                                                # its own position (no back-drive), so only
                                                # energize to move. Avoids the ~0.5 A idle hold.
            print("Valve: init OK (motor de-energized at idle) -- must home before moves")
            state = "unknown"
            return
        print("Valve: init attempt", attempt, "-- setup ACK failed")
        time.sleep(0.2)
    print("Valve: init FAILED after retries -- giving up")
    state = "error"
    last_error = "init_setup_failed"


# ── Breath oscillator ──────────────────────────────────────────────────────

def _breath_phase_and_dphase(now):
    """Return (phase_rad, dphase_per_sec) for the breath cycle at `now`.
    phase rises from -pi/2 (valley) through +pi/2 (peak) during the 'rise'
    portion of the cycle, then continues to +3pi/2 (back to valley) during
    'fall'. Asymmetric: rise takes _breath_skew * period, fall takes the rest.
    """
    period_ms = max(100, _breath_period_ms)
    s = max(0.05, min(0.95, _breath_skew))
    t_in_cycle = _ticks_diff(now, _breath_phase_start_ms) % period_ms
    rise_ms = int(s * period_ms)
    if t_in_cycle < rise_ms:
        phase = -math.pi / 2 + math.pi * t_in_cycle / rise_ms
        dphase_per_sec = math.pi / (rise_ms / 1000.0)
    else:
        fall_ms = period_ms - rise_ms
        phase = math.pi / 2 + math.pi * (t_in_cycle - rise_ms) / fall_ms
        dphase_per_sec = math.pi / (fall_ms / 1000.0)
    return phase, dphase_per_sec


def _breath_speed_dir(now):
    """0xF6 speed_dir byte for the breath at `now`: feed-forward (sine derivative) plus a
    drift term restoring toward the baseline target_pos_steps. None when velocity rounds to
    gear 0 -- the natural rest at the valley/peak. The hard-stop guard uses motor_pos_steps,
    which is re-grounded on the encoder at each valley."""
    phase, dphase_per_sec = _breath_phase_and_dphase(now)
    osc_frac_per_sec = _breath_amplitude * math.cos(phase) * dphase_per_sec
    if open_steps > 0 and BREATH_DRIFT_TAU_S > 0:
        drift_frac_per_sec = (target_pos_steps - motor_pos_steps) / open_steps / BREATH_DRIFT_TAU_S
    else:
        drift_frac_per_sec = 0.0
    velocity_sps = (osc_frac_per_sec + drift_frac_per_sec) * open_steps
    gear = int(round(abs(velocity_sps) / STEPS_PER_GEAR_PER_SEC))
    if gear == 0:
        return None
    if gear > BREATH_MAX_GEAR:
        gear = BREATH_MAX_GEAR
    direction = DIR_TOWARD_OPEN if velocity_sps > 0 else DIR_TOWARD_CLOSED
    if direction == DIR_TOWARD_OPEN and motor_pos_steps >= open_steps:
        return None
    if direction == DIR_TOWARD_CLOSED and motor_pos_steps <= 0:
        return None
    return direction | (gear & 0x7F)


def _integrate_breath_motion(now):
    """Dead-reckon motor_pos_steps from the last 0xF6 gear held over the elapsed tick.
    Between valleys this is the only position estimate; it's re-grounded on the encoder at
    each valley, so the accumulated error stays bounded to one cycle (no unbounded drift)."""
    global motor_pos_steps
    if _breath_last_speed_dir is None or _breath_last_update_ms == 0:
        return
    elapsed_ms = _ticks_diff(now, _breath_last_update_ms)
    gear = _breath_last_speed_dir & 0x7F
    steps_moved = (gear * elapsed_ms) // 2     # gear * 500 steps/s * ms / 1000
    if _breath_last_speed_dir & 0x80:
        motor_pos_steps += steps_moved
    else:
        motor_pos_steps -= steps_moved
    motor_pos_steps = max(0, min(open_steps, motor_pos_steps))


def _enter_breathing(now):
    """Transition idle -> breathing. Caller must ensure motor is at or near
    target_pos_steps and homed."""
    global state, _breath_phase_start_ms, _breath_last_update_ms, _breath_last_speed_dir
    global _breath_last_good_read_ms, _breath_prev_t, _breath_at_valley
    _send(bytes([CMD_ENABLE, 0x01]))   # energize for continuous breath motion
    state = "breathing"
    _breath_phase_start_ms = now
    _breath_last_update_ms = 0       # fire the first tick immediately
    _breath_last_speed_dir = None
    _breath_prev_t = 0
    _breath_at_valley = False
    _breath_last_good_read_ms = now  # fresh feedback baseline for the silence backstop
    print(f"Valve: breathing -- baseline={target_pos_steps} A={_breath_amplitude:.3f} T={_breath_period_ms}ms skew={_breath_skew:.2f}")


def _service_breath(now):
    """Valley-grounded smooth 0xF6. Drive a continuous 0xF6 (BARE send -- the MKS won't
    reliably ACK it mid-motion and we don't need the ACK), dead-reckoning position between
    reads. Once per cycle at the valley -- detected by the cycle wrap (bottom of the
    waveform), not by a chance rest -- halt the motor and, the next tick, take a stopped
    0x30 read to re-ground motor_pos_steps. That bounds drift and gives the bottom dwell."""
    global _breath_last_update_ms, _breath_last_speed_dir, _breath_prev_t, _breath_at_valley
    if _pending_cmd is not None:
        return                                          # valley read in flight
    if _ticks_diff(now, _breath_last_update_ms) < BREATH_UPDATE_MS:
        return
    _integrate_breath_motion(now)                       # estimate from the last 0xF6 gear
    _breath_last_update_ms = now
    if _breath_at_valley:                               # halted last tick -> settled -> read
        _breath_at_valley = False
        _send_and_expect(bytes([CMD_READ_ENCODER]), "breath_read")
        return
    period_ms = max(100, _breath_period_ms)
    t = _ticks_diff(now, _breath_phase_start_ms) % period_ms
    crossed_valley = t < _breath_prev_t                 # cycle wrapped -> reached the bottom
    _breath_prev_t = t
    if crossed_valley:
        _breath_at_valley = True
        _send(bytes([CMD_CONSTANT_SPEED, 0x00]))        # halt at the bottom for a clean read
        _breath_last_speed_dir = None
        return
    sd = _breath_speed_dir(now)
    if sd != _breath_last_speed_dir:                    # velocity changed -> fresh 0xF6 (bare)
        _send(bytes([CMD_CONSTANT_SPEED, sd if sd is not None else 0x00]))
        _breath_last_speed_dir = sd


def _on_breath_read(raw):
    """Valley read landed (motor stopped at the bottom -> reliable): re-ground
    motor_pos_steps on the absolute encoder. This is the only breath encoder read.
    (A phase re-anchor here -- to remove the ~0.05 dwell-induced center offset -- was tried
    but only tested under a loose coupler, so it's unvalidated; re-attempt after the repair.)"""
    global motor_pos_steps, _breath_last_good_read_ms
    motor_pos_steps = max(0, min(open_steps, _encoder_pos_steps(raw)))
    _breath_last_good_read_ms = supervisor.ticks_ms()


def _exit_breath_for_jump(new_step_target):
    """Big target shift while breathing: send 0xF7, transition through
    'exiting_breath', which lands in 'idle' once the STOP ACK arrives.
    _parse_response then issues the 0xFD to _pending_jump_target."""
    global state, _pending_jump_target
    _pending_jump_target = new_step_target
    state = "exiting_breath"
    _send_and_expect(bytes([CMD_STOP]), "breath_stop")


def _exit_breath_to_idle():
    """User-requested breath disable. Stop the motor; transition to idle on ACK."""
    global state
    state = "exiting_breath"
    _send_and_expect(bytes([CMD_STOP]), "breath_stop_idle")


# ── Service loop ───────────────────────────────────────────────────────────

def service():
    global pending_target, last_target_ms
    global state, last_error, target_pos_steps, _pending_move, _pending_sync_target

    now = supervisor.ticks_ms()

    _drain_uart_into_buf()
    while len(_rx_buf) >= 3:
        if not _parse_response():
            break
    _check_timeout()
    _check_mks_silence(now)

    # Fire a move staged by _issue_move once its post-ENABLE settle has elapsed.
    if (_pending_move is not None and _pending_cmd is None
            and _ticks_diff(now, _move_settle_at) < 0x1FFFFFFF):
        mv = _pending_move
        _pending_move = None
        _send_and_expect(mv, "move_start")

    if state == "homing":
        # The inchworm is ACK-driven via the home_* chain. Only the timeout
        # backstop lives here: de-energize if a home wedges (lost ACK, etc).
        elapsed = _ticks_diff(now, _home_started_ms)
        if elapsed >= HOME_TIMEOUT_MS:
            print(f"Valve: homing timed out after {elapsed} ms -- motor disabled")
            cmd_stop()
            _send(bytes([CMD_ENABLE, 0x00]))   # cmd_stop only halts motion; drop windings
            state = "error"
            last_error = "home_timeout"

    elif state == "homing_finalize":
        _service_finalize(now)

    elif state == "breathing":
        # Small baseline shifts just move target_pos_steps; the per-tick 0xFD in
        # _on_breath_read tracks it. Big jumps interrupt with STOP + 0xFD + auto-resume.
        if (pending_target is not None
                and _ticks_diff(now, last_target_ms) >= TARGET_MIN_MS):
            new_target = pending_target
            pending_target = None
            last_target_ms = now
            new_step_target = int(round(new_target * open_steps))
            delta_frac = abs(new_step_target - target_pos_steps) / open_steps if open_steps > 0 else 0
            if delta_frac > BREATH_BIG_JUMP:
                target_pos_steps = new_step_target
                _exit_breath_for_jump(new_step_target)
            else:
                target_pos_steps = new_step_target
        _service_breath(now)

    elif (state == "idle"
            and _pending_cmd is None
            and pending_target is not None
            and _ticks_diff(now, last_target_ms) >= TARGET_MIN_MS):
        target = pending_target
        pending_target = None
        last_target_ms = now
        _pending_sync_target = int(round(target * open_steps))
        if homed:
            # Ground motor_pos_steps on a fresh encoder read before computing the move
            # delta; _on_sync_read dispatches the move (or breathes if already at target).
            _send_and_expect(bytes([CMD_READ_ENCODER]), "sync_read")
        else:
            _dispatch_pending_target()   # _issue_move will refuse (not homed)


# ── MQTT outbound ──────────────────────────────────────────────────────────

def _pos_fraction():
    if open_steps <= 0:
        return 0.0
    return motor_pos_steps / open_steps


def _target_fraction():
    if open_steps <= 0:
        return 0.0
    return target_pos_steps / open_steps


def _status_json():
    d = {
        "state": state,
        "pos": round(_pos_fraction(), 3),
        "target": round(_target_fraction(), 3),
        "homed": homed,
        "stalled": state == "stalled",
        "last_error": last_error,
    }
    if state in ("homing", "homing_finalize"):
        # Inchworm telemetry: last encoder raw + per-step |delta|. hd ~= cruise while
        # advancing, drops toward 0 on contact. (Per-step cruise/count print to USB.)
        d["hraw"] = _home_last_raw
        d["hd"] = _home_last_delta
    return json.dumps(d)


def get_publish_messages():
    global _last_status_ms, _last_actual_ms
    now = supervisor.ticks_ms()
    msgs = []
    interval = STATUS_MOVE_MS if state in ("moving", "homing", "homing_finalize") else STATUS_IDLE_MS
    if _ticks_diff(now, _last_status_ms) >= interval:
        _last_status_ms = now
        msgs.append((TOPIC_VALVE_STATUS, _status_json()))
    if _ticks_diff(now, _last_actual_ms) >= ACTUAL_MS:
        _last_actual_ms = now
        msgs.append((TOPIC_VALVE_ACTUAL, str(round(_pos_fraction(), 3))))
    return msgs

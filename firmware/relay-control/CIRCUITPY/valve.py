# valve.py -- Motorized needle valve via MKS SERVO42C-MT V1.1.2
# UART binary protocol on GP4 (TX) / GP5 (RX) at 115200 baud.
#
# Position convention (motor steps):
#   motor_pos_steps = 0           -> fully open  (homing stop, MKS zero is set here)
#   motor_pos_steps = open_steps  -> fully closed
# MQTT 0.0 = closed, 1.0 = open. motor_pos_steps = (1.0 - target) * open_steps.
#
# Architecture: the SERVO42C is a closed-loop FOC servo. We trust it to reach
# commanded positions and tell us when via the status=2 "run complete" ACK
# emitted by 0xFD (the 0xFD command emits TWO responses: status=1 starting,
# status=2 complete; see manual §5.5.5 / §6.4). Encoder (0x30) is only read
# during homing for stall detection; in normal operation the actual position
# we publish is the commanded motor_pos_steps.

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
CMD_SET_ZERO_MODE   = 0x90
CMD_SET_ZERO        = 0x91
CMD_SET_ZERO_SPEED  = 0x92
CMD_SET_ZERO_DIR    = 0x93
CMD_RETURN_ZERO     = 0x94
CMD_SET_ACC         = 0xA4   # accel ramp; lower = smoother speed transitions on 0xF6
CMD_CONSTANT_SPEED  = 0xF6   # continuous motion; [speed_dir]; gear=0 stops
CMD_ENABLE          = 0xF3
CMD_STOP            = 0xF7
CMD_MOVE_POS        = 0xFD   # two-stage response: status=1 starting -> status=2 complete (or 0 fail)

# ── Valve config ───────────────────────────────────────────────────────────
OPEN_STEPS      = 16000      # default; ~5 turns @ 16x microstep (3200 steps/rev)
MOVE_SPEED      = 20         # 0xFD speed gear; Vrpm ≈ 9.375 × gear ≈ 187 rpm at 16x
CURRENT_GEAR    = 0x01       # 200 mA floor (CR_UART auto-adjusts under load)
MICROSTEP       = 16

# 0xFD direction bit (OR'd with speed). Spec: bit7=0 CW, bit7=1 CCW.
# On THIS hardware (empirically verified): bit7=0 drives the valve toward the
# open stop, bit7=1 drives toward closed. Swap these if the motor moves the
# wrong way for a given target.
DIR_TOWARD_OPEN   = 0x00
DIR_TOWARD_CLOSED = 0x80

# 0x93 zero direction. Spec: 0x00=CW, 0x01=CCW. On this hardware, 0x00 drives
# toward the open stop. Swap to 0x01 if homing goes the wrong way.
HOME_DIR        = 0x00
HOME_ZERO_SPEED = 0x03       # 0..4 (0=fastest)

# ── MQTT topics ────────────────────────────────────────────────────────────
TOPIC_VALVE_TARGET    = b"bush/fire/valve/target"
TOPIC_VALVE_HOME      = b"bush/fire/valve/home"
TOPIC_VALVE_STOP      = b"bush/fire/valve/stop"
TOPIC_VALVE_CALIBRATE = b"bush/fire/valve/calibrate"
TOPIC_VALVE_BREATH    = b"bush/fire/valve/breath"
TOPIC_VALVE_ACTUAL    = b"bush/fire/valve/actual"
TOPIC_VALVE_STATUS    = b"bush/fire/valve/status"
TOPIC_VALVE_ONLINE    = b"bush/fire/valve/online"

ALL_VALVE_TOPICS = [
    TOPIC_VALVE_TARGET,
    TOPIC_VALVE_HOME,
    TOPIC_VALVE_STOP,
    TOPIC_VALVE_CALIBRATE,
    TOPIC_VALVE_BREATH,
]

# ── State ──────────────────────────────────────────────────────────────────
# states: "unknown", "homing", "idle", "moving", "stalled", "error"
state                = "unknown"
homed                = False
motor_pos_steps      = 0     # 0 = open, open_steps = closed (post-homing reference)
target_pos_steps     = 0
move_in_flight_delta = 0     # signed: +ve = toward closed
open_steps           = OPEN_STEPS
last_error           = None

pending_target  = None
last_target_ms  = 0
TARGET_MIN_MS   = 100        # don't issue moves faster than 10 Hz

_rx_buf         = bytearray()
_pending_cmd    = None
_cmd_sent_ms    = 0
CMD_TIMEOUT_MS  = 500
MOVE_TIMEOUT_MS = 30000      # worst-case full-travel move at slow speeds

# Homing
_home_started_ms   = 0
_home_last_poll_ms = 0
_home_last_raw     = 0
_home_last_move_ms = 0
HOME_TIMEOUT_MS    = 30000
HOME_POLL_MS       = 500
HOME_STALL_MS      = 3000    # encoder unchanged this long -> declare stalled

# Post-stall finalization (state="homing_finalize").
# After encoder-stall detection, we run a small fire-and-forget sequence:
# SET_ZERO -> SET_PROTECT(on) -> ENABLE. We don't track ACKs (they'd race with
# late RETURN_ZERO/STOP ACKs from the abort), and we space them out so the
# main loop stays responsive for relay-pulse timing.
_finalize_step    = 0
_finalize_next_ms = 0
FINALIZE_STEP_MS  = 100

# Post-finalize settle (state="post_home_settle"). Time for late ACKs from
# the fire-and-forget finalize sequence to arrive and be consumed as strays
# by _parse_response before we accept a move. Without this, ENABLE's late
# [E0 01 E1] ACK is byte-identical to a 0xFD status=1 and gets eaten as the
# first move's start ACK -- the real start ACK then becomes "move_done with
# status=1" and the state machine errors out.
_post_home_settle_until = 0
POST_HOME_SETTLE_MS     = 500

# Publication
_last_status_ms = 0
_last_actual_ms = 0
STATUS_IDLE_MS  = 1000
STATUS_MOVE_MS  = 200
ACTUAL_MS       = 250

# Checksum-fail logging: rate-limit so 0xF6 noise during breathing doesn't
# flood the USB CDC console at 10+ lines/sec. Accumulate counts, log a
# summary every CKSUM_LOG_MS with the last failed head bytes.
_cksum_fail_count    = 0
_cksum_fail_last_ms  = 0
_cksum_fail_last_head = None
_cksum_fail_last_cmd  = None
CKSUM_LOG_MS         = 2000

# ── Breath oscillator ──────────────────────────────────────────────────────
# Skewed-sine modulation around target_pos_steps as the center. Realized by
# periodic 0xF6 (constant speed) updates tracking the sine derivative; the MKS
# interpolates between speed levels via SET_ACC. Sentiment-driven baseline
# changes blend in via a drift term in the velocity calc; large jumps
# (> BREATH_BIG_JUMP fraction of full travel) interrupt with 0xF7 + 0xFD +
# auto-resume on move_done.
#
# Configurable at runtime via JSON to bush/fire/valve/breath.
_breath_enabled    = True
_breath_amplitude  = 0.04         # peak deviation from baseline, fraction of full travel
_breath_period_ms  = 5000
_breath_skew       = 0.5          # rise fraction of period; 0.5 = symmetric sine

_breath_phase_start_ms = 0
_breath_last_update_ms = 0
_breath_last_speed_dir = None     # last 0xF6 byte sent, or None if motor stopped
_pending_jump_target   = 0        # step target queued during exiting_breath
BREATH_UPDATE_MS       = 100      # 0xF6 cadence
BREATH_BIG_JUMP        = 0.10     # baseline change > this fraction triggers 0xFD jump
BREATH_ACC_VALUE       = 286      # 0xA4 SET_ACC; 286 = min documented = gentlest ramp
BREATH_MAX_GEAR        = 30       # safety clamp on speed_gear
BREATH_DRIFT_TAU_S     = 2.0      # baseline-shift drift time constant
STEPS_PER_GEAR_PER_SEC = 500      # at 16x microstep: gear * 9.375 RPM = gear * 500 steps/s


def _ticks_diff(later, earlier):
    return (later - earlier) & 0x3FFFFFFF


def _log_cksum_fail(head_bytes, cmd):
    global _cksum_fail_count, _cksum_fail_last_ms
    global _cksum_fail_last_head, _cksum_fail_last_cmd
    _cksum_fail_count += 1
    _cksum_fail_last_head = head_bytes
    _cksum_fail_last_cmd = cmd
    now = supervisor.ticks_ms()
    if _ticks_diff(now, _cksum_fail_last_ms) >= CKSUM_LOG_MS:
        print(f"Valve: cksum fail x{_cksum_fail_count} (last head={head_bytes}, cmd={cmd})")
        _cksum_fail_count = 0
        _cksum_fail_last_ms = now


# ── Packet helpers ─────────────────────────────────────────────────────────

def _checksum(data):
    return sum(data) & 0xFF


def _send(cmd_bytes):
    pkt = bytes([MKS_ADDR]) + cmd_bytes
    pkt = pkt + bytes([_checksum(pkt)])
    uart.write(pkt)


def _send_and_expect(cmd_bytes, label):
    global _pending_cmd, _cmd_sent_ms
    _pending_cmd = label
    _cmd_sent_ms = supervisor.ticks_ms()
    _send(cmd_bytes)


def _drain_uart_buffer():
    """Discard pending bytes from both hardware UART and software _rx_buf.
    Used at transition points where stale ACKs from prior commands could be
    mis-parsed as the next command's response -- MKS ACKs are all 3-byte
    [addr, status, crc], and ENABLE/STOP/RETURN_ZERO success ACKs are
    byte-identical to a 0xFD move's status=1, so a late one in the buffer
    looks like our new move already started."""
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
    global _breath_last_speed_dir
    # Send STOP and let its ACK arrive via the normal parser.
    _send_and_expect(bytes([CMD_STOP]), "stop")
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
    """Issue a relative 0xFD move toward absolute step_target. Returns True if sent."""
    global target_pos_steps, move_in_flight_delta
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
    direction = DIR_TOWARD_CLOSED if delta > 0 else DIR_TOWARD_OPEN
    speed_dir = direction | (MOVE_SPEED & 0x7F)
    pulse_bytes = abs(delta).to_bytes(4, "big")
    print(f"Valve: move {motor_pos_steps} -> {step_target} (d={delta} pulses)")
    # Drain stale bytes so a late ACK from a prior command can't be eaten as
    # this move's status=1.
    _drain_uart_buffer()
    _send_and_expect(bytes([CMD_MOVE_POS, speed_dir]) + pulse_bytes, "move_start")
    return True


# ── Homing chain ──────────────────────────────────────────────────────────
# Every command in the homing sequence is sent one at a time via
# _send_and_expect; each ACK in _parse_response chains to the next step.
# This avoids ACK alignment problems from batched bare-_send setup commands.

def cmd_home():
    """Begin homing sequence. Asynchronous: completes via the response chain
    and the encoder-stall fallback in service()."""
    global state, homed, _home_started_ms, pending_target
    global _home_last_raw, _home_last_move_ms, _home_last_poll_ms
    global _breath_last_speed_dir

    # If breathing was active, stop the 0xF6 motion first so homing's RETURN_ZERO
    # isn't fighting a continuous-speed command.
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
    # Drain any late ACKs from a prior interrupted move (e.g. a delayed
    # status=2 after a stall) so the homing chain's first ACK isn't eaten.
    _drain_uart_buffer()
    _home_started_ms = supervisor.ticks_ms()
    _home_last_raw = 0
    _home_last_move_ms = 0
    _home_last_poll_ms = 0

    print("Valve: homing -- chain start (clear latched protect)")
    _send_and_expect(bytes([CMD_CLEAR_PROTECT]), "home_clear_protect")


def _complete_homing_by_stall():
    """Encoder hasn't moved for HOME_STALL_MS. Enter the finalize state, which
    runs SET_ZERO -> SET_PROTECT(on) -> ENABLE as fire-and-forget sub-steps in
    service(). We don't track ACKs here because the late RETURN_ZERO/STOP ACKs
    race with our commands during this transition."""
    global state, homed, motor_pos_steps, target_pos_steps, move_in_flight_delta
    global _pending_cmd, _finalize_step, _finalize_next_ms
    print(f"Valve: encoder stalled at raw={_home_last_raw} -- entering finalize")
    homed = True
    motor_pos_steps = 0
    target_pos_steps = 0
    move_in_flight_delta = 0
    state = "homing_finalize"
    _pending_cmd = None
    _finalize_step = 0
    # Send STOP immediately. The 300 ms settle lets MKS abort RETURN_ZERO and
    # emit whatever ACKs it owes us; the parser drops them as strays.
    _send(bytes([CMD_STOP]))
    _finalize_next_ms = (supervisor.ticks_ms() + 300) & 0x3FFFFFFF


def _service_finalize(now):
    """Advance the post-stall finalize state machine. Called from service()."""
    global state, _finalize_step, _finalize_next_ms
    if _ticks_diff(now, _finalize_next_ms) >= 0x1FFFFFFF:
        return  # not time yet
    step = _finalize_step
    if step == 0:
        _send(bytes([CMD_SET_ZERO, 0x00]))
        _finalize_step = 1
        _finalize_next_ms = (now + FINALIZE_STEP_MS) & 0x3FFFFFFF
    elif step == 1:
        _send(bytes([CMD_SET_PROTECT, 0x01]))
        _finalize_step = 2
        _finalize_next_ms = (now + FINALIZE_STEP_MS) & 0x3FFFFFFF
    elif step == 2:
        _send(bytes([CMD_ENABLE, 0x01]))
        _finalize_step = 3
        # ENABLE's ACK can take longer than the other steps because FOC comes
        # online here. Give it ~300 ms so the ACK is definitely in the RX
        # buffer before we drain on the next tick.
        _finalize_next_ms = (now + 300) & 0x3FFFFFFF
    else:
        # ENABLE's success ACK [E0 01 E1] is byte-identical to a 0xFD
        # status=1; without aggressive draining it survives into the next
        # service() iteration and gets eaten as our first real move's "start"
        # ACK, which then puts the parser one ACK ahead -> "unexpected
        # status=1". Drain now, then sit in post_home_settle for a few hundred
        # ms while _parse_response keeps soaking up anything that trickles in.
        global _post_home_settle_until
        _drain_uart_buffer()
        state = "post_home_settle"
        _post_home_settle_until = (now + POST_HOME_SETTLE_MS) & 0x3FFFFFFF
        print(f"Valve: homing finalize complete -- post-home settle ({POST_HOME_SETTLE_MS} ms)")


# ── Response parsing ───────────────────────────────────────────────────────

def _parse_response():
    global _rx_buf, _pending_cmd, _cmd_sent_ms
    global state, homed, last_error, motor_pos_steps, move_in_flight_delta
    global _home_last_raw, _home_last_move_ms, _pending_jump_target

    if len(_rx_buf) < 3:
        return False
    if _rx_buf[0] != MKS_ADDR:
        _rx_buf = _rx_buf[1:]
        return True

    cmd = _pending_cmd

    if cmd == "read_encoder":
        if len(_rx_buf) < 8:
            return False
        if _checksum(_rx_buf[0:7]) != _rx_buf[7]:
            # Strip 1 byte and let the alignment loop find the next 0xE0,
            # rather than dropping a full 8 bytes of potentially-real data.
            _log_cksum_fail(list(_rx_buf[0:8]), "read_encoder")
            _rx_buf = _rx_buf[1:]
            return True
        carry = struct.unpack(">i", bytes(_rx_buf[1:5]))[0]
        value = struct.unpack(">H", bytes(_rx_buf[5:7]))[0]
        raw = (carry << 16) | value
        if state == "homing":
            delta = raw - _home_last_raw
            if delta != 0:
                _home_last_move_ms = supervisor.ticks_ms()
            _home_last_raw = raw
            print(f"Valve: homing raw={raw} d={delta}")
        _rx_buf = _rx_buf[8:]
        _pending_cmd = None
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
            _pending_cmd = None
            motor_pos_steps += move_in_flight_delta
            motor_pos_steps = max(0, min(open_steps, motor_pos_steps))
            move_in_flight_delta = 0
            state = "idle"
            print(f"Valve: move complete, pos={motor_pos_steps}")
            # Auto-enter breathing if enabled and homed. This handles both
            # initial target-from-rest and post-jump resume.
            if _breath_enabled and homed:
                _enter_breathing(supervisor.ticks_ms())
        elif status == 0:
            _pending_cmd = None
            state = "stalled"
            last_error = "move_stalled"
            move_in_flight_delta = 0
            print("Valve: 0xFD complete status=0 -- motor stalled")
        elif status == 1:
            # Spurious "started" status received while expecting "complete".
            # Happens when a stale [E0 01 E1] (RETURN_ZERO/STOP/finalize ACK
            # buffered internally by the MKS) leaks into the move's response
            # stream. The real status=2 should still be coming -- keep waiting.
            print("Valve: ignoring stray status=1 during move_done")
        else:
            _pending_cmd = None
            state = "error"
            last_error = "move_bad_done"
            move_in_flight_delta = 0
            print(f"Valve: 0xFD complete unexpected status={status}")
        return True

    if cmd == "breath":
        # 0xF6 ACK: status may be 0 or 1 -- we don't escalate on either since
        # the next update cycle will issue a fresh 0xF6 anyway.
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
        # User disabled breathing. STOP ACK -> idle.
        _pending_cmd = None
        state = "idle"
        return True

    # ── Homing chain (pre-RETURN_ZERO) ─────────────────────────────────
    if cmd == "home_clear_protect":
        _pending_cmd = None
        _send_and_expect(bytes([CMD_SET_PROTECT, 0x00]), "home_protect_off")
        return True

    if cmd == "home_protect_off":
        _pending_cmd = None
        _send_and_expect(bytes([CMD_SET_ZERO_MODE, 0x01]), "home_zmode")
        return True

    if cmd == "home_zmode":
        _pending_cmd = None
        _send_and_expect(bytes([CMD_SET_ZERO_DIR, HOME_DIR]), "home_zdir")
        return True

    if cmd == "home_zdir":
        _pending_cmd = None
        _send_and_expect(bytes([CMD_SET_ZERO_SPEED, HOME_ZERO_SPEED]), "home_zspeed")
        return True

    if cmd == "home_zspeed":
        _pending_cmd = None
        # All setup is done. Fire RETURN_ZERO without expecting an ACK --
        # 0x94's response is unreliable at low current and we detect
        # completion via the encoder-stall fallback in service().
        print(f"Valve: homing -- RETURN_ZERO (dir=0x{HOME_DIR}, speed={HOME_ZERO_SPEED})")
        _send(bytes([CMD_RETURN_ZERO, 0x00]))
        # No pending command; service() now polls the encoder.
        return True

    if cmd == "stop":
        _pending_cmd = None
        return True

    # Unmatched / stray ACK (e.g. post-stall finalize sends, or late ACKs
    # from RETURN_ZERO/STOP after the abort). Silently drop.
    return True


def _drain_uart_into_buf():
    global _rx_buf
    data = uart.read(64)
    if data:
        _rx_buf.extend(data)


def _check_timeout():
    global _pending_cmd, state, last_error
    if _pending_cmd is None:
        return
    timeout = MOVE_TIMEOUT_MS if _pending_cmd == "move_done" else CMD_TIMEOUT_MS
    if _ticks_diff(supervisor.ticks_ms(), _cmd_sent_ms) < timeout:
        return
    print(f"Valve: UART timeout waiting for {_pending_cmd}")
    # read_encoder polls and breath updates are best-effort; abandon without
    # escalating. The next tick of the homing/breathing loop will reissue.
    if _pending_cmd in ("read_encoder", "breath"):
        _pending_cmd = None
        return
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
              and _blocking_setup(bytes([CMD_SET_MICROSTEP, MICROSTEP])) == 1
              and _blocking_setup(bytes([CMD_SET_CURRENT, CURRENT_GEAR])) == 1
              and _blocking_setup(bytes([CMD_SET_PROTECT, 0x01])) == 1
              and _blocking_setup(bytes([CMD_SET_ACC, acc_hi, acc_lo])) == 1
              and _blocking_setup(bytes([CMD_ENABLE, 0x01])) == 1)
        if ok:
            print("Valve: init OK -- must home before moves")
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
    """Compute the 0xF6 speed_dir byte for current phase, or None if velocity
    rounds to gear 0 (motor should stop). Includes a baseline-drift term so
    that small target_pos_steps changes blend into the oscillation."""
    phase, dphase_per_sec = _breath_phase_and_dphase(now)
    # Oscillator velocity in MQTT-fraction units (positive = opening).
    osc_frac_per_sec = _breath_amplitude * math.cos(phase) * dphase_per_sec
    # Drift toward baseline. Step convention: motor_pos_steps low = open.
    # Fraction error = (1 - motor_pos/open_steps) - (1 - target_pos/open_steps)
    #                = (target_pos - motor_pos) / open_steps
    # Negative because target_pos > motor_pos means motor is more open than
    # baseline -> we need to close (decrease fraction).
    if open_steps > 0 and BREATH_DRIFT_TAU_S > 0:
        drift_frac_per_sec = -(target_pos_steps - motor_pos_steps) / open_steps / BREATH_DRIFT_TAU_S
    else:
        drift_frac_per_sec = 0.0
    velocity_frac_per_sec = osc_frac_per_sec + drift_frac_per_sec
    # Convert to step velocity (sign flip: opening means motor_pos_steps decreasing).
    velocity_steps_per_sec = -velocity_frac_per_sec * open_steps
    abs_sps = abs(velocity_steps_per_sec)
    gear = int(round(abs_sps / STEPS_PER_GEAR_PER_SEC))
    if gear == 0:
        return None
    if gear > BREATH_MAX_GEAR:
        gear = BREATH_MAX_GEAR
    direction = DIR_TOWARD_CLOSED if velocity_steps_per_sec > 0 else DIR_TOWARD_OPEN
    return direction | (gear & 0x7F)


def _integrate_breath_motion(now):
    """Update motor_pos_steps based on the last 0xF6 gear+direction held for the
    elapsed time. Called once per BREATH_UPDATE_MS tick before computing the
    next gear. Bounded drift estimate; ground-truthed by encoder reads on
    mode transitions."""
    global motor_pos_steps
    if _breath_last_speed_dir is None or _breath_last_update_ms == 0:
        return
    elapsed_ms = _ticks_diff(now, _breath_last_update_ms)
    gear = _breath_last_speed_dir & 0x7F
    # gear * 500 steps/sec * elapsed_ms / 1000  ==  gear * elapsed_ms / 2
    steps_moved = (gear * elapsed_ms) // 2
    if _breath_last_speed_dir & 0x80:
        motor_pos_steps += steps_moved
    else:
        motor_pos_steps -= steps_moved
    motor_pos_steps = max(0, min(open_steps, motor_pos_steps))


def _enter_breathing(now):
    """Transition idle -> breathing. Caller must ensure motor is at or near
    target_pos_steps and homed."""
    global state, _breath_phase_start_ms, _breath_last_update_ms, _breath_last_speed_dir
    state = "breathing"
    _breath_phase_start_ms = now
    _breath_last_update_ms = 0      # force immediate update on first tick
    _breath_last_speed_dir = None
    print(f"Valve: breathing -- baseline={target_pos_steps} A={_breath_amplitude:.3f} T={_breath_period_ms}ms skew={_breath_skew:.2f}")


def _service_breath(now):
    """Advance the breath oscillator. Called from service() while state=breathing."""
    global _breath_last_update_ms, _breath_last_speed_dir
    if _pending_cmd is not None:
        return                                          # prior 0xF6 ACK still pending
    if _ticks_diff(now, _breath_last_update_ms) < BREATH_UPDATE_MS:
        return
    _integrate_breath_motion(now)
    _breath_last_update_ms = now
    new_speed_dir = _breath_speed_dir(now)
    if new_speed_dir == _breath_last_speed_dir:
        return
    if new_speed_dir is None:
        _send_and_expect(bytes([CMD_CONSTANT_SPEED, 0x00]), "breath")
    else:
        _send_and_expect(bytes([CMD_CONSTANT_SPEED, new_speed_dir]), "breath")
    _breath_last_speed_dir = new_speed_dir


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
    global pending_target, last_target_ms, _home_last_poll_ms
    global state, last_error, target_pos_steps

    now = supervisor.ticks_ms()

    _drain_uart_into_buf()
    while len(_rx_buf) >= 3:
        if not _parse_response():
            break
    _check_timeout()

    if state == "homing":
        elapsed = _ticks_diff(now, _home_started_ms)
        if elapsed >= HOME_TIMEOUT_MS:
            print(f"Valve: homing timed out after {elapsed} ms")
            cmd_stop()
            state = "error"
            last_error = "home_timeout"
        elif _pending_cmd is None:
            if (_home_last_move_ms > 0
                    and _ticks_diff(now, _home_last_move_ms) >= HOME_STALL_MS):
                _complete_homing_by_stall()
            elif _ticks_diff(now, _home_last_poll_ms) >= HOME_POLL_MS:
                _home_last_poll_ms = now
                _send_and_expect(bytes([CMD_READ_ENCODER]), "read_encoder")

    elif state == "homing_finalize":
        _service_finalize(now)

    elif state == "post_home_settle":
        # _parse_response at the top of service() is consuming any stragglers.
        # Just wait for the settle window to expire, then a final drain.
        if _ticks_diff(now, _post_home_settle_until) < 0x1FFFFFFF:
            return
        _drain_uart_buffer()
        state = "idle"
        print("Valve: ready for moves")

    elif state == "breathing":
        # Small target shifts blend via the velocity drift term in _breath_speed_dir.
        # Big jumps interrupt with STOP + 0xFD + auto-resume on move_done.
        if (pending_target is not None
                and _ticks_diff(now, last_target_ms) >= TARGET_MIN_MS):
            new_target = pending_target
            pending_target = None
            last_target_ms = now
            new_step_target = int(round((1.0 - new_target) * open_steps))
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
        step_target = int(round((1.0 - target) * open_steps))
        if motor_pos_steps == step_target and _breath_enabled and homed:
            target_pos_steps = step_target
            _enter_breathing(now)
        else:
            _issue_move(step_target)


# ── MQTT outbound ──────────────────────────────────────────────────────────

def _pos_fraction():
    if open_steps <= 0:
        return 0.0
    return 1.0 - (motor_pos_steps / open_steps)


def _target_fraction():
    if open_steps <= 0:
        return 0.0
    return 1.0 - (target_pos_steps / open_steps)


def _status_json():
    return json.dumps({
        "state": state,
        "pos": round(_pos_fraction(), 3),
        "target": round(_target_fraction(), 3),
        "homed": homed,
        "stalled": state == "stalled",
        "last_error": last_error,
    })


def get_publish_messages():
    global _last_status_ms, _last_actual_ms
    now = supervisor.ticks_ms()
    msgs = []
    interval = STATUS_MOVE_MS if state == "moving" else STATUS_IDLE_MS
    if _ticks_diff(now, _last_status_ms) >= interval:
        _last_status_ms = now
        msgs.append((TOPIC_VALVE_STATUS, _status_json()))
    if _ticks_diff(now, _last_actual_ms) >= ACTUAL_MS:
        _last_actual_ms = now
        msgs.append((TOPIC_VALVE_ACTUAL, str(round(_pos_fraction(), 3))))
    return msgs

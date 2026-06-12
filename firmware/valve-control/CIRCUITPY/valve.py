# valve.py -- Motorized needle valve via MKS SERVO42D over CAN (MCP2515/XL2515).
#
# Rewritten from the SERVO42C driver for the 42D, whose serial protocol differs:
#   frame   : FA <addr> <func> <data...> <sum&0xFF>   (reply headed 0xFB)
#   baud    : 38400 (42D default), addr 0x01
#   workmode: must be SR_vFOC (0x82 05) or serial motion is ignored
#   current : raw mA via 0x83 (uint16), not the 42C's 200 mA gears
#   motion  : 0xF6 speed / 0xFD rel-pulses carry a 12-bit RPM + an accel byte
#   encoder : 0x31 int48 multi-turn "addition" value, 16384 counts/rev
#   homing  : the 42D's locked-rotor (stallguard) detection finds the seat --
#             no inchworm encoder-delta dance; drive in, catch the stall.
#
# Position convention (motor steps):
#   motor_pos_steps = 0           -> closed seat margin (zero set here at homing)
#   motor_pos_steps = open_steps  -> fully open
# MQTT 0.0 = closed, 1.0 = open. motor_pos_steps = target * open_steps.
#
# motor_pos_steps is DERIVED from the 0x31 encoder, never dead-reckoned: homing
# captures the zero + sign, then every move/nudge/breath-valley re-reads 0x31.
#
# !! Values tagged VERIFY were carried/estimated from the 42C and the 42D manual.
#    CAN closed loop bench-proven on the 42D 2026-06-10 (direction, current=torque,
#    encoder, protection-seek homing); the RPM/accel envelope is still uncharacterized.

import board
import supervisor
import json
import math
import struct
import time

# Transport to the MKS is created by the board glue (code.py). This build talks CAN
# (the SERVO42D_CAN variant): the glue sets valve.Message (adafruit_mcp2515.canio.Message)
# and valve.can (MCP2515 bus) before init(). CAN frame data = [func, params..., crc],
# crc = (CAN_ID + func + params) & 0xFF, arbitration id = ADDR. See PROTOCOL.md.
can           = None     # MCP2515 CAN bus object (board glue sets it)
Message       = None     # adafruit_mcp2515.canio.Message class (board glue sets it)
_can_listener = None
uart = None              # legacy RS485/UART transport (unused on the CAN build)

# ── MKS SERVO42D RS485/UART protocol ─────────────────────────────────────────
TX_HEAD = 0xFA               # downlink (host -> servo) frame head
RX_HEAD = 0xFB               # uplink (servo -> host) frame head
ADDR    = 0x01               # slave address (42D default)

CMD_READ_ENCODER    = 0x31   # int48 multi-turn addition value (absolute ground truth)
CMD_READ_SHAFT_PROT = 0x3E   # -> 1 = locked-rotor protection latched (stalled)
CMD_RELEASE_PROT    = 0x3D   # clear the locked-rotor latch
CMD_READ_EN         = 0x3A   # -> 1 enabled / 0 disabled
CMD_RUN_STATUS      = 0xF1   # -> 0 fail,1 stop,2 accel,3 decel,4 full,5 homing
CMD_CALIBRATE       = 0x80
CMD_SET_WORKMODE    = 0x82   # 5 = SR_vFOC (serial closed-loop FOC)
CMD_SET_CURRENT     = 0x83   # uint16 mA, big-endian
CMD_SET_MICROSTEP   = 0x84
CMD_SET_PROTECT     = 0x88   # locked-rotor protection 1=on/0=off
CMD_SET_ZERO        = 0x92   # set current axis as zero (no motion)
CMD_SET_RESPOND     = 0x8C   # [respon][active]: 1,1 = full two-stage motion replies
CMD_ENABLE          = 0xF3
CMD_CONSTANT_SPEED  = 0xF6   # speed mode: [dir|spd_hi][spd_lo][acc]; spd=0 stops
CMD_STOP            = 0xF7   # emergency stop (all modes)
CMD_MOVE_POS        = 0xFD   # rel pulses: [dir|spd_hi][spd_lo][acc][pulse int32 BE]

WORKMODE_SR_VFOC    = 0x05

# Reply payload byte-count per function code (frame = 3 head + payload + 1 crc).
_RESP_PLEN = {
    0x31: 6,   # int48 addition
    0x30: 6,   # int32 carry + uint16 value (unused, kept for completeness)
    0x39: 4, 0x32: 2, 0x33: 4,
    0x3E: 1, 0x3D: 1, 0x3A: 1, 0xF1: 1, 0x34: 1,
    0x80: 1, 0x82: 1, 0x83: 1, 0x84: 1, 0x88: 1, 0x92: 1, 0x91: 1, 0x90: 1,
    0xF3: 1, 0xF6: 1, 0xF7: 1, 0xFD: 1, 0xFE: 1,
}

# ── Valve config ───────────────────────────────────────────────────────────
MICROSTEP       = 16
_USTEP          = MICROSTEP // 16
STEPS_PER_REV   = 200 * MICROSTEP          # 3200 microsteps/rev at 16x
OPEN_STEPS      = 11200 * _USTEP           # bench-cal 2026-06-10: open hard stop ~11737 from the
                                            # home margin; keep ~540 below it (open stop JAMS at high
                                            # current -- never drive into it). Full travel ~3.76 rev.

VALVE_CURRENT_MA = 1000      # operating run current mA (max 3000). Free travel needs only ~200;
                             # 1000 gives headroom for high RPM/accel. Tune live via maxtorque;
                             # operation stays within [0, OPEN_STEPS] so it never rams a stop.
                              # force on a stall-home; raise if it can't move, lower if the
                              # seat contact is too hard.
MOVE_RPM        = 40         # VERIFY. 0xFD/0xF6 cruise speed (RPM; 42D speed IS rpm at >=16x)
MOVE_ACC        = 2          # VERIFY. accel byte: each unit-rpm step takes (256-acc)*50us;
                              # acc=0 is instant (no ramp), small acc = gentle ramp.

# Runtime-tunable motion limits (bush/fire/valve/limits; in-memory, defaults above).
# Homing speeds/currents are deliberately NOT runtime-settable.
move_rpm        = MOVE_RPM
move_acc        = MOVE_ACC

# 0xFD/0xF6 direction bit (b7 of the speed_hi byte). 42D manual: 0=CCW, 1=CW.
DIR_TOWARD_OPEN   = 0x00     # bench-verified 2026-06-10 (was 0x80)
DIR_TOWARD_CLOSED = 0x80     # bench-verified 2026-06-10 (was 0x00); encoder counts UP closing
ENC_SIGN_DEFAULT  = -1       # bench-verified: dir 0x00 (open) LOWERS raw. Used when zeroing
                             # without a seat seek -- +1 here makes every open-ward move read
                             # back as 0 and ratchet into the jamming OPEN stop.

# ── MQTT topics (also the BLE/serial line-protocol keys) ─────────────────────
TOPIC_VALVE_TARGET    = b"bush/fire/valve/target"
TOPIC_VALVE_HOME      = b"bush/fire/valve/home"
TOPIC_VALVE_STOP      = b"bush/fire/valve/stop"
TOPIC_VALVE_CALIBRATE = b"bush/fire/valve/calibrate"
TOPIC_VALVE_BREATH    = b"bush/fire/valve/breath"
TOPIC_VALVE_MAXTORQUE = b"bush/fire/valve/maxtorque"   # repurposed on 42D: sets run current (mA)
TOPIC_VALVE_NUDGE     = b"bush/fire/valve/nudge"
TOPIC_VALVE_ACTUAL    = b"bush/fire/valve/actual"
TOPIC_VALVE_STATUS    = b"bush/fire/valve/status"
TOPIC_VALVE_ONLINE    = b"bush/fire/valve/online"
TOPIC_VALVE_PONG      = b"bush/fire/valve/pong"        # stream clock-sync reply
TOPIC_VALVE_STREAMPOS = b"bush/fire/valve/streampos"   # executed position (open-loop sync check)
TOPIC_VALVE_LIMITS    = b"bush/fire/valve/limits"      # in: JSON motion limits; empty = query
TOPIC_VALVE_TRACE     = b"bush/fire/valve/trace"       # in: in-flight 0x31 trace interval ms (0 = off)
TOPIC_VALVE_LIMITS_ACK = b"bush/fire/valve/limits_ack" # out: one-shot limits readback
TOPIC_VALVE_MOVED     = b"bush/fire/valve/moved"       # out: per-move result JSON
TOPIC_VALVE_TRACEPT   = b"bush/fire/valve/tracept"     # out: "<ticks_ms> <pos_steps>"
TOPIC_VALVE_STREAMEND = b"bush/fire/valve/streamend"   # out: stream divergence JSON

ALL_VALVE_TOPICS = [
    TOPIC_VALVE_TARGET,
    TOPIC_VALVE_HOME,
    TOPIC_VALVE_STOP,
    TOPIC_VALVE_CALIBRATE,
    TOPIC_VALVE_BREATH,
    TOPIC_VALVE_MAXTORQUE,
    TOPIC_VALVE_NUDGE,
    TOPIC_VALVE_LIMITS,
    TOPIC_VALVE_TRACE,
]

# ── State ──────────────────────────────────────────────────────────────────
# states: "unknown","homing","homing_finalize","idle","moving","breathing",
#         "exiting_breath","stalled","error","nudging"
state                = "unknown"
homed                = False
motor_pos_steps      = 0
target_pos_steps     = 0
move_in_flight_delta = 0     # signed: +ve = toward open (telemetry / timeout fallback)
_nudge_delta         = 0
open_steps           = OPEN_STEPS
last_error           = None

# Absolute position from the 0x31 multi-turn addition encoder.
ENC_PER_REV          = 16384.0                          # +0x4000 per turn
ENC_PER_STEP         = ENC_PER_REV / STEPS_PER_REV      # 5.12 counts/microstep at 16x
_enc_zero_raw        = 0
_enc_sign            = 1     # +1 if raw rises toward OPEN, set at homing
_pending_sync_target = 0

pending_target  = None
last_target_ms  = 0
TARGET_MIN_MS   = 100

_rx_buf         = bytearray()
_pending_cmd    = None       # label of the reply we're waiting on (state tracking)
_cmd_sent_ms    = 0
CMD_TIMEOUT_MS  = 500
MOVE_TIMEOUT_MS = 8000

# A 0x31 reply is consumed by _pending_cmd ONLY when the label is one of these;
# any other 0x31 reply is an in-flight trace point (never clobbers a pending wait).
_ENC_LABELS = ("sync_read", "move_sync", "nudge_sync", "stall_sync", "stream_end_read",
               "home_seed", "home_contact_read", "home_backoff_read", "home_zero_seed",
               "breath_read", "stream_seed")

# A fresh encoder read while homed must agree with bookkeeping to within ~a rev;
# a bigger gap = the 42D rebooted (brownout resets its accumulator + volatile config
# while `homed` stays true) or the clutch slipped. Either way the zero is a lie.
GROUND_LOST_STEPS = STEPS_PER_REV

_move_timeout_ms = MOVE_TIMEOUT_MS   # per-move, rpm-aware (set when the move is staged)
_move_meta       = None              # in-flight move record for the `moved` telemetry
_current_ma      = VALVE_CURRENT_MA  # last commanded run current (re-asserted on ground loss)

# A 0xFD right after ENABLE doesn't execute reliably; energize, stage, fire after settle.
_pending_move   = None
_move_settle_at = 0
MOVE_SETTLE_MS  = 120

# ── Homing ───────────────────────────────────────────────────────────────────
# Homing is ENABLED (bench-proven 2026-06-10): `home` ramp-drives into the closed seat
# and zeros at a backed-off margin. Set True to fall back to "current shaft = 0" (no seek).
HOMING_DISABLED = False

# ── Seat homing (lockrotor-protection seek, bench-proven 2026-06-10) ─────────
# Protection stays ON (it is our seat detector AND the operating jam net). cmd_home()
# GENTLY drives toward the closed seat; the 42D latches (0x3E == 1) on the stall at the
# seat. Homing is deliberately GENTLE: a healthy needle valve is smooth across its whole
# travel, so a low seek current traverses freely and only stalls at the seat. We do NOT
# bump current to force past resistance -- forcing is what shreds the internals (creating
# the sticky spots), so rough homing perpetuates the damage. A worn valve that latches
# early reports an error; it is never pushed through. On the seat latch: back off a margin
# toward open, verify it moved, and zero there. Runs BLOCKING (homing is a boot/rare op).
# The encoder-flatline "method b" is in git history.
HOME_RPM             = 30 * _USTEP   # free-travel approach speed
HOME_ACC             = 2
HOME_MAX_PULSES      = 6 * STEPS_PER_REV   # bound the seek (~6 rev); exceeds full open->seat
HOME_SEEK_CUR        = 300           # mA -- GENTLE: enough to traverse a healthy valve, soft at seat
HOME_BACKOFF_CUR     = 400           # mA to lift off the seat
HOME_BACKOFF_STEPS   = 300 * _USTEP  # margin off the seat = position 0
HOME_BACKOFF_MIN_FRAC = 0.5
HOME_TIMEOUT_MS      = 45000
_home_seed_raw       = 0
_home_contact_raw  = 0
_home_started_ms   = 0
_motion_ctx        = None    # what a 0xFD reply means: move/nudge/home_drive/home_backoff

# Post-contact finalize (state="homing_finalize"): release, backoff, zero, seed.
_finalize_step    = 0
_finalize_next_ms = 0
FINALIZE_STEP_MS  = 120

# Publication
_last_status_ms = 0
_last_actual_ms = 0
STATUS_IDLE_MS  = 1000
STATUS_MOVE_MS  = 200
ACTUAL_MS       = 250

# Checksum-fail logging (rate-limited).
_cksum_fail_count   = 0
_cksum_fail_last_ms = 0
CKSUM_LOG_MS        = 2000

# ── Breath oscillator ────────────────────────────────────────────────────────
_breath_enabled    = True
_breath_amplitude  = 0.04
_breath_period_ms  = 5000
_breath_skew       = 0.5

_breath_phase_start_ms = 0
_breath_last_update_ms = 0
_breath_last_rpm       = None    # last signed RPM sent (+ = toward open), or None if stopped
_breath_prev_t         = 0
_breath_at_valley      = False
_pending_jump_target   = 0
BREATH_UPDATE_MS       = 100
BREATH_ACC             = 0       # snap: acc 8 can't reach commanded speed within the 100 ms
                                 # update at fast periods -- the lag rectifies into a walk
                                 # (bench 2026-06-12, 1.2 s breath walked 0.3 span in 6 s)
                                  # speed target within the interval, so not too gentle.
MKS_SILENCE_LIMIT_MS   = 2000
_breath_last_good_read_ms = 0
BREATH_BIG_JUMP        = 0.10
BREATH_ENTER_DEADBAND  = 5 * _USTEP
BREATH_MAX_RPM         = 120 * _USTEP   # VERIFY
breath_max_rpm         = BREATH_MAX_RPM # runtime-tunable (bush/fire/valve/limits)
BREATH_DRIFT_TAU_S     = 2.0

# ── Streamed waveform playback ───────────────────────────────────────────────
# A host streams a dense position waveform ahead of an audio playhead; the firmware
# buffers it and plays each sample on ITS OWN clock (open-loop -- the host waveform is
# the position authority, no encoder grounding), so BLE delivery jitter doesn't move
# motion timing. Same 0xF6 velocity-follow as breath, just an arbitrary trajectory.
# Binary frame: SENTINEL TYPE LEN(2 BE) PAYLOAD CRC(sum&0xFF). See bush_cue/wire.py.
STREAM_SENTINEL = 0xF5
SF_START   = 0x01   # rate_hz(u16) base_play_ms(u32)
SF_SAMPLES = 0x02   # start_index(u32) count(u16) positions(u8*count; 0..255 = 0..open)
SF_STOP    = 0x03
SF_PING    = 0x05   # token(u16) -> pong telemetry
STREAM_CAP      = 256              # ring capacity (~8.5 s @ 30 Hz)
STREAM_MAX_RPM  = 600 * _USTEP     # VERIFY. cap on stream slew (42D allows up to 3000)
stream_max_rpm  = STREAM_MAX_RPM   # runtime-tunable (bush/fire/valve/limits)
STREAM_TELEM_MS = 200              # executed-position telemetry cadence

_stream_buf      = bytearray(STREAM_CAP)
_stream_max_idx  = -1
_stream_played   = -1
_stream_rate     = 30
_stream_base_ms  = 0
_stream_epoch    = 0
_stream_last_telem_ms = 0
_stream_out      = []              # queued outbound telemetry (pong/streampos/moved/...)

# In-flight encoder trace (bush/fire/valve/trace) -- bare 0x31 polls while moving/streaming.
_trace_interval_ms = 0             # 0 = off
_trace_last_ms     = 0
_trace_inflight_ms = 0             # ticks when the bare poll went out; 0 = none outstanding
TRACE_STALE_MS     = 300

# Stream runaway guard: open-loop follow cannot bound resonant overshoot (bench
# 2026-06-12: a 15 Hz tone commanded inside 0.3..0.5 of span walked the shaft to
# 1.41 and into the open stop). Poll the encoder while streaming; if the SHAFT
# (not the bookkeeping) leaves the window by more than the margin, cut the stream.
STREAM_GUARD_MS     = 50
STREAM_GUARD_MARGIN = 400          # steps past [0, open_steps]; production stop is ~537 past top
_guard_last_ms      = 0

# Stall watch for the 0xF6 follow modes (stream/breath): 0xF6 replies are fire-and-forget,
# so a lockrotor latch there is silent -- poll 0x3E and fail loudly instead.
_prot_poll_last_ms = 0
PROT_POLL_MS       = 400

# Post-stream encoder ground-truth read (divergence report + re-ground).
_stream_cmd_frac     = 0.0         # dead-reckoned authority captured at stream end/stall
_stream_end_read_at  = 0           # ticks deadline for the settle-then-read; 0 = none
STREAM_END_SETTLE_MS = 400

# ── RGB status LED (XIAO onboard, active-low) -- shows actuation level ────────
_led          = None
_led_mode     = None     # "rgb" (XIAO) | "digital" (Pico W single LED) | None
_led_last_ms  = 0
LED_UPDATE_MS = 60


def _ticks_diff(later, earlier):
    return (later - earlier) & 0x3FFFFFFF


def _queue_out(item):
    """Queue outbound telemetry, bounded: with the host link down nothing drains the
    queue, and a tracing stream would otherwise grow it until the node OOMs."""
    if len(_stream_out) < 256:
        _stream_out.append(item)


def _cancel_stream_end_read():
    global _stream_end_read_at
    _stream_end_read_at = 0


def _encoder_pos_steps(raw):
    """Absolute valve position in steps from a 0x31 raw read. 0 = homed margin."""
    return int(round((raw - _enc_zero_raw) * _enc_sign / ENC_PER_STEP))


def _steps_to_rpm(steps_per_sec):
    return abs(steps_per_sec) * 60.0 / STEPS_PER_REV


def _calc_move_timeout(pulses, rpm, acc):
    """Expected 0xFD duration (cruise + accel ramp) x1.5 + 2 s slack, floored at the
    old fixed budget so short moves keep their old timeout."""
    rpm = max(1, rpm)
    cruise_ms = pulses * 60000 // (rpm * STEPS_PER_REV)
    ramp_ms = int(rpm * (256 - acc) * 0.05) if acc > 0 else 0
    t = (3 * (cruise_ms + ramp_ms)) // 2 + 2000
    return min(max(t, MOVE_TIMEOUT_MS), 300000)


def _log_cksum_fail(head_bytes):
    global _cksum_fail_count, _cksum_fail_last_ms
    _cksum_fail_count += 1
    now = supervisor.ticks_ms()
    if _ticks_diff(now, _cksum_fail_last_ms) >= CKSUM_LOG_MS:
        print(f"Valve: cksum/frame fail x{_cksum_fail_count} (last={head_bytes})")
        _cksum_fail_count = 0
        _cksum_fail_last_ms = now


# ── Packet helpers ───────────────────────────────────────────────────────────

def _checksum(data):
    return sum(data) & 0xFF


def _send(body):
    """Send a command over CAN. body = [func, param0, ...]; frame data = body + crc,
    crc = (CAN_ID + sum(body)) & 0xFF, arbitration id = ADDR."""
    if can is None or Message is None:
        return
    crc = (ADDR + sum(body)) & 0xFF
    try:
        can.send(Message(id=ADDR, data=bytes(body) + bytes([crc])))
    except Exception as e:
        print("Valve: CAN send failed:", e)


def _send_and_expect(body, label):
    global _pending_cmd, _cmd_sent_ms
    _pending_cmd = label
    _cmd_sent_ms = supervisor.ticks_ms()
    _send(body)


def _speed_bytes(rpm):
    rpm = max(0, min(3000, int(round(rpm))))
    return (rpm >> 8) & 0x0F, rpm & 0xFF


def _speed_body(func, dir_bit, rpm, acc, pulses=None):
    hi, lo = _speed_bytes(rpm)
    b = bytes([func, (dir_bit & 0x80) | hi, lo, acc & 0xFF])
    if pulses is not None:
        # CAN 0xFD/0xFE carry a 24-bit pulse count (3 bytes), not the RS485 32-bit, so
        # the frame [func, dir|hi, lo, acc, p2, p1, p0, crc] fits one 8-byte CAN frame.
        b = b + (int(pulses) & 0xFFFFFF).to_bytes(3, "big")
    return b


def _ensure_listener():
    global _can_listener
    if _can_listener is None and can is not None:
        try:
            _can_listener = can.listen(timeout=0.0)   # non-blocking, receive all
        except Exception as e:
            print("Valve: CAN listen failed:", e)
    return _can_listener


def _drain_can():
    """Discard any pending inbound CAN frames."""
    lis = _ensure_listener()
    if lis is None:
        return
    try:
        while lis.in_waiting():
            lis.receive()
    except Exception:
        pass


def _poll_can():
    """Dispatch all pending inbound CAN reply frames (called every service tick)."""
    lis = _ensure_listener()
    if lis is None:
        return
    try:
        n = lis.in_waiting()
    except Exception:
        return
    for _ in range(n):
        try:
            msg = lis.receive()
        except Exception:
            break
        if msg is None:
            break
        _handle_can_msg(msg)


def _handle_can_msg(msg):
    """One CAN reply frame -> dispatch. data = [func, status/data..., crc],
    crc = (id + func + data) & 0xFF."""
    data = bytes(getattr(msg, "data", b"") or b"")
    if len(data) < 2:
        return
    if (msg.id + sum(data[:-1])) & 0xFF != data[-1]:
        _log_cksum_fail(list(data[:3]))
        return
    _dispatch(data[0], data[1:-1])


# ── Commands ─────────────────────────────────────────────────────────────────

def cmd_stop():
    global state, target_pos_steps, move_in_flight_delta, homed, pending_target
    global _breath_last_rpm, _pending_move, _motion_ctx, _move_meta
    global _stream_max_idx, _stream_played
    _send_and_expect(bytes([CMD_STOP]), "stop")
    _send(bytes([CMD_ENABLE, 0x00]))
    _stream_max_idx = -1
    _stream_played = -1
    _pending_move = None
    _motion_ctx = None
    _move_meta = None        # aborted move -- never emit it as a `moved`
    move_in_flight_delta = 0
    target_pos_steps = motor_pos_steps
    pending_target = None
    _breath_last_rpm = None
    if state in ("moving", "homing", "homing_finalize"):
        homed = False
        state = "unknown"
    elif state != "error":
        state = "idle"
    print("Valve: STOP")


def _issue_move(step_target):
    """Stage a relative 0xFD toward absolute step_target: energize now, fire after settle."""
    global target_pos_steps, move_in_flight_delta, state, _pending_move, _move_settle_at
    global _motion_ctx, _move_timeout_ms, _move_meta
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
    _motion_ctx = None              # normal move (distinct from home/nudge contexts)
    _cancel_stream_end_read()       # new motion invalidates a pending divergence read
    direction = DIR_TOWARD_OPEN if delta > 0 else DIR_TOWARD_CLOSED
    print(f"Valve: move {motor_pos_steps} -> {step_target} (d={delta})")
    _move_meta = {"cmd": delta, "pre": motor_pos_steps, "rpm": move_rpm, "acc": move_acc,
                  "fired": 0, "ms": 0}
    _move_timeout_ms = _calc_move_timeout(abs(delta), move_rpm, move_acc)
    _pending_move = _speed_body(CMD_MOVE_POS, direction, move_rpm, move_acc, abs(delta))
    _send(bytes([CMD_ENABLE, 0x01]))
    _move_settle_at = (supervisor.ticks_ms() + MOVE_SETTLE_MS) & 0x3FFFFFFF
    state = "moving"
    return True


# ── Encoder-grounded move/nudge finalization ─────────────────────────────────

def _ground_lost(pos, expect, where):
    """The encoder frame moved out from under us (42D brownout/reboot, or slip).
    Kill all motion authority, re-assert the volatile motor config, demand a re-home."""
    global homed, state, last_error, _breath_enabled, _breath_last_rpm
    global pending_target, _pending_move, _motion_ctx
    global move_in_flight_delta, _nudge_delta, _stream_max_idx, _stream_played
    homed = False
    state = "error"
    last_error = "ground_lost"
    _breath_enabled = False
    _breath_last_rpm = None
    pending_target = None
    _pending_move = None
    _motion_ctx = None
    _emit_moved(None, 1)            # no-op unless a move was in flight
    move_in_flight_delta = 0
    _nudge_delta = 0
    _stream_max_idx = -1
    _stream_played = -1
    _send(bytes([CMD_CONSTANT_SPEED, 0x00, 0x00, 0x00]))
    _send(bytes([CMD_STOP]))
    _send(bytes([CMD_ENABLE, 0x00]))
    _send(bytes([CMD_SET_WORKMODE, WORKMODE_SR_VFOC]))
    _send(bytes([CMD_SET_MICROSTEP, MICROSTEP & 0xFF]))
    _send(bytes([CMD_SET_CURRENT, (_current_ma >> 8) & 0xFF, _current_ma & 0xFF]))
    _send(bytes([CMD_SET_PROTECT, 0x01]))
    print("Valve: GROUND LOST at %s -- enc %d vs expected %d; motor rebooted or "
          "slipped, disabled until re-home" % (where, pos, expect))


def _ground_ok(raw, expect_steps, where):
    """Fresh encoder read vs bookkeeping. Returns the position, or None after
    declaring ground_lost. Unhomed reads pass through (setup jogging)."""
    pos = _encoder_pos_steps(raw)
    if not homed or abs(pos - expect_steps) <= GROUND_LOST_STEPS:
        return pos
    _ground_lost(pos, expect_steps, where)
    return None


def _dispatch_pending_target():
    global target_pos_steps
    step_target = _pending_sync_target
    if _breath_enabled and homed and abs(step_target - motor_pos_steps) <= BREATH_ENTER_DEADBAND:
        target_pos_steps = step_target
        _enter_breathing(supervisor.ticks_ms())
    else:
        _issue_move(step_target)


def _on_sync_read(raw):
    global motor_pos_steps
    pos = _ground_ok(raw, motor_pos_steps, "target_sync")
    if pos is None:
        return                      # ground lost -- the queued target never dispatches
    motor_pos_steps = pos           # unclamped: off-window truth makes recovery moves correct
    _dispatch_pending_target()


def _finalize_move_to(pos):
    global motor_pos_steps, move_in_flight_delta, state
    motor_pos_steps = pos
    move_in_flight_delta = 0
    print(f"Valve: move complete, pos={motor_pos_steps}")
    if _breath_enabled and homed:
        _enter_breathing(supervisor.ticks_ms())
    else:
        _send(bytes([CMD_STOP]))
        _send(bytes([CMD_ENABLE, 0x00]))
        state = "idle"


def _finalize_nudge_to(pos):
    global motor_pos_steps, _nudge_delta, state
    motor_pos_steps = pos
    _nudge_delta = 0
    _send(bytes([CMD_STOP]))
    _send(bytes([CMD_ENABLE, 0x00]))
    state = "idle"
    print(f"Valve: nudge done pos~={motor_pos_steps} -- de-energized")


def _move_meta_done():
    """Stamp the in-flight move's duration at its terminal 0xFD reply."""
    if _move_meta is not None:
        _move_meta["ms"] = _ticks_diff(supervisor.ticks_ms(), _move_meta["fired"])


def _emit_moved(pos, stall):
    """Queue the per-move result. pos = post-move encoder steps, or None if the read failed."""
    global _move_meta
    m = _move_meta
    _move_meta = None
    if m is None:
        return
    _queue_out((TOPIC_VALVE_MOVED, json.dumps({
        "ms": m["ms"], "cmd": m["cmd"],
        "enc": None if pos is None else pos - m["pre"],
        "rpm": m["rpm"], "acc": m["acc"], "stall": stall})))


def _on_stall_sync(raw):
    """Re-ground position after a stall (0x31 replies even while latched)."""
    global motor_pos_steps, target_pos_steps
    pos = _encoder_pos_steps(raw)
    _emit_moved(pos, 1)
    motor_pos_steps = pos
    target_pos_steps = pos


# ── Stallguard homing ────────────────────────────────────────────────────────

def _fake_home_at_zero():
    """No homing: declare the CURRENT shaft position to be 0, then go idle/homed.
    Reads the encoder to ground the zero reference; falls back to raw 0 if no reply."""
    global _enc_zero_raw, _enc_sign, motor_pos_steps, target_pos_steps, homed, state
    raw = _blocking_read_encoder()
    if raw is not None:
        _enc_zero_raw = raw
    _enc_sign = ENC_SIGN_DEFAULT
    motor_pos_steps = 0
    target_pos_steps = 0
    homed = True
    state = "idle"
    print(f"Valve: homing disabled -- boot/home position = 0 (enc_zero_raw={_enc_zero_raw})")


def cmd_home_here():
    """Bench zero: declare the CURRENT shaft = 0 (no seat seek) -- for rigs that must not
    be seat-homed (worn valve, bare motor). Bare `home` keeps the real gentle seek."""
    global pending_target, _pending_move, _pending_cmd, _motion_ctx, _breath_last_rpm
    global last_error, _move_meta, move_in_flight_delta, _nudge_delta
    global _trace_inflight_ms, _stream_end_read_at, _stream_max_idx, _stream_played
    pending_target = None
    _pending_move = None
    _pending_cmd = None
    _motion_ctx = None
    _move_meta = None
    _breath_last_rpm = None
    move_in_flight_delta = 0
    _nudge_delta = 0
    _trace_inflight_ms = 0
    _stream_end_read_at = 0
    _stream_max_idx = -1
    _stream_played = -1
    _send(bytes([CMD_STOP]))
    _send(bytes([CMD_RELEASE_PROT]))     # a stalled bench recovers here too
    _send(bytes([CMD_ENABLE, 0x00]))
    last_error = None
    _drain_can()
    _fake_home_at_zero()


def cmd_home():
    """Seat homing by lockrotor protection (bench-proven 2026-06-10; BLOCKING ~10-30 s).
    Protection ON: drive toward the closed seat until the 42D latches (0x3E == 1) on the
    hard stall. A latch within HOME_MIN_TRAVEL_RAW of the start is a sticky spot, not the
    seat -> release, bump current, keep going. On the seat latch: release, back off a margin
    toward open, verify it moved, and zero there. (HOMING_DISABLED -> just re-zero in place.)
    Blocks the main loop while homing -- the valve isn't operating then."""
    global state, homed, pending_target, _pending_move, _pending_cmd, _motion_ctx, last_error
    global _enc_zero_raw, _enc_sign, motor_pos_steps, target_pos_steps, _breath_last_rpm
    global _move_meta, _stream_end_read_at
    if state == "breathing" or _breath_last_rpm is not None:
        _send(bytes([CMD_STOP]))
        _breath_last_rpm = None
    pending_target = None
    _pending_move = None
    _pending_cmd = None      # a stale move_done wait would refuse moves until its timeout
    _motion_ctx = None
    _move_meta = None
    _stream_end_read_at = 0
    if HOMING_DISABLED:
        _drain_can()
        _fake_home_at_zero()
        return
    homed = False
    state = "homing"
    _send(bytes([CMD_STOP]))
    _send(bytes([CMD_SET_PROTECT, 0x01]))       # protection ON = the seat detector
    _send(bytes([CMD_RELEASE_PROT]))            # clear any stale latch
    _drain_can()
    _set_current(HOME_SEEK_CUR)
    start_raw = _blocking_read_encoder()
    if start_raw is None:
        start_raw = 0
    print("Valve: homing -- gentle protection-seek into the closed seat @ %d mA" % HOME_SEEK_CUR)
    _send(bytes([CMD_ENABLE, 0x01]))
    time.sleep(MOVE_SETTLE_MS / 1000.0)
    _send(_speed_body(CMD_MOVE_POS, DIR_TOWARD_CLOSED, HOME_RPM, HOME_ACC, HOME_MAX_PULSES))
    t0 = time.monotonic()
    seat_raw = None
    while time.monotonic() - t0 < HOME_TIMEOUT_MS / 1000.0:
        if _blocking_read_status(CMD_READ_SHAFT_PROT) == 1:       # latched = seat
            seat_raw = _blocking_read_encoder()
            if seat_raw is None:
                seat_raw = start_raw
            print("Valve: home seat latch raw=%s (moved %d)" % (seat_raw, abs(seat_raw - start_raw)))
            break
        time.sleep(0.01)
    _send(bytes([CMD_STOP]))
    if seat_raw is None:
        _send(bytes([CMD_ENABLE, 0x00]))
        state = "error"
        last_error = "home_no_seat"
        print("Valve: homing FAILED -- no seat latch in time")
        return
    _send(bytes([CMD_RELEASE_PROT]))            # clear the seat latch before backing off
    _set_current(HOME_BACKOFF_CUR)
    _send(bytes([CMD_ENABLE, 0x01]))
    time.sleep(MOVE_SETTLE_MS / 1000.0)
    _send(_speed_body(CMD_MOVE_POS, DIR_TOWARD_OPEN, HOME_RPM, HOME_ACC, HOME_BACKOFF_STEPS))
    tb = time.monotonic()
    while time.monotonic() - tb < 2.5:
        time.sleep(0.05)
        _blocking_read_encoder(timeout_ms=100)
    _send(bytes([CMD_STOP]))
    _send(bytes([CMD_ENABLE, 0x00]))
    zero_raw = _blocking_read_encoder()
    if zero_raw is not None and abs(zero_raw - seat_raw) < HOME_BACKOFF_STEPS * ENC_PER_STEP * HOME_BACKOFF_MIN_FRAC:
        state = "error"
        last_error = "home_stuck"
        print("Valve: backoff too small (%d) -- STUCK" % abs(zero_raw - seat_raw))
        return
    _enc_zero_raw = zero_raw if zero_raw is not None else seat_raw
    _enc_sign = -1                              # dir 0x00 opens and LOWERS raw -> position rises toward open
    motor_pos_steps = 0
    target_pos_steps = 0
    homed = True
    state = "idle"
    _set_current(VALVE_CURRENT_MA)              # restore operating current (protection stays ON)
    print("Valve: homed -- zero=%s (margin off seat), enc_sign=-1, ready" % _enc_zero_raw)


def _home_begin_drive(seed_raw):
    """Seed captured; fire the single stall-seeking 0xFD toward the closed seat.
    (This async chain is dead since cmd_home went blocking; kept for a revival.)"""
    global _home_seed_raw, _motion_ctx, _move_settle_at, _pending_move
    _home_seed_raw = seed_raw
    print(f"Valve: homing -- seed raw={seed_raw}, driving into seat ({HOME_MAX_PULSES} max)")
    _motion_ctx = "home_drive"
    _pending_move = _speed_body(CMD_MOVE_POS, DIR_TOWARD_CLOSED, HOME_RPM, HOME_ACC, HOME_MAX_PULSES)
    _send(bytes([CMD_ENABLE, 0x01]))
    _move_settle_at = (supervisor.ticks_ms() + MOVE_SETTLE_MS) & 0x3FFFFFFF


def _home_on_contact():
    """Stall/contact at the seat. Release the latch and start finalize (backoff + zero)."""
    global state, _finalize_step, _finalize_next_ms, _motion_ctx
    print("Valve: home contact (stall) -- finalize")
    _motion_ctx = None
    _send(bytes([CMD_STOP]))
    _send(bytes([CMD_RELEASE_PROT]))
    _send(bytes([CMD_ENABLE, 0x00]))     # drop windings; finalize re-enables for backoff
    state = "homing_finalize"
    _finalize_step = 0
    _finalize_next_ms = (supervisor.ticks_ms() + FINALIZE_STEP_MS) & 0x3FFFFFFF


def _service_finalize(now):
    """Post-contact: read contact pos, back off toward open, verify, zero, seed."""
    global _finalize_step, _finalize_next_ms, _motion_ctx, _move_settle_at, _pending_move
    if _ticks_diff(now, _finalize_next_ms) >= 0x1FFFFFFF:
        return
    if _finalize_step == 0:
        _send_and_expect(bytes([CMD_READ_ENCODER]), "home_contact_read")
        _finalize_step = 99   # wait for the read; reads re-arm the chain
    elif _finalize_step == 1:
        _send(bytes([CMD_ENABLE, 0x01]))
        _finalize_step = 2
        _finalize_next_ms = (now + FINALIZE_STEP_MS) & 0x3FFFFFFF
    elif _finalize_step == 2:
        print(f"Valve: homing -- backing off {HOME_BACKOFF_STEPS} toward open")
        _motion_ctx = "home_backoff"
        _pending_move = _speed_body(CMD_MOVE_POS, DIR_TOWARD_OPEN, HOME_RPM, HOME_ACC,
                                    HOME_BACKOFF_STEPS)
        _move_settle_at = (now + MOVE_SETTLE_MS) & 0x3FFFFFFF
        _finalize_step = 99


def _on_home_contact_read(raw):
    global _home_contact_raw, _enc_sign, _finalize_step, _finalize_next_ms
    _home_contact_raw = raw
    _enc_sign = 1 if (_home_seed_raw - raw) > 0 else -1   # raw fell while closing -> rises toward open
    print(f"Valve: contact raw={raw} (enc_sign={_enc_sign})")
    _finalize_step = 1
    _finalize_next_ms = (supervisor.ticks_ms() + FINALIZE_STEP_MS) & 0x3FFFFFFF


def _home_verify_backoff(raw):
    global state, last_error
    moved = abs(raw - _home_contact_raw)
    expected = HOME_BACKOFF_STEPS * ENC_PER_STEP
    if expected and moved < expected * HOME_BACKOFF_MIN_FRAC:
        _send(bytes([CMD_ENABLE, 0x00]))
        state = "error"
        last_error = "home_stuck"
        print(f"Valve: backoff moved only {moved} (exp ~{int(expected)}) -- STUCK, disabled")
        return
    print(f"Valve: backoff verified ({moved} cts) -- zeroing")
    _finish_homing()


def _finish_homing():
    global motor_pos_steps, target_pos_steps, move_in_flight_delta
    _send(bytes([CMD_STOP]))
    _send(bytes([CMD_SET_ZERO]))         # 0x92: this backed-off margin is now zero
    motor_pos_steps = 0
    target_pos_steps = 0
    move_in_flight_delta = 0
    _send(bytes([CMD_ENABLE, 0x00]))
    _drain_can()
    _send_and_expect(bytes([CMD_READ_ENCODER]), "home_zero_seed")
    print("Valve: homed (zero = margin off seat, de-energized) -- seeding enc zero")


def _on_home_zero_seed(raw):
    global _enc_zero_raw, state, _motion_ctx, homed
    _enc_zero_raw = raw
    _motion_ctx = None
    homed = True
    state = "idle"
    print(f"Valve: enc zero seeded raw={raw} sign={_enc_sign} -- homed, ready")


# ── Response parsing ─────────────────────────────────────────────────────────

def _read_int48(payload):
    raw = int.from_bytes(payload, "big")
    if raw >= (1 << 47):
        raw -= (1 << 48)
    return raw


def _parse_response():
    global _rx_buf, _pending_cmd
    if len(_rx_buf) < 5:                  # smallest frame: FB addr func status crc
        return False
    if _rx_buf[0] != RX_HEAD:
        del _rx_buf[0]
        return True
    func = _rx_buf[2]
    plen = _RESP_PLEN.get(func)
    if plen is None:
        _log_cksum_fail(list(_rx_buf[0:3]))
        del _rx_buf[0]
        return True
    total = 3 + plen + 1
    if len(_rx_buf) < total:
        return False
    if _checksum(_rx_buf[0:total - 1]) != _rx_buf[total - 1]:
        _log_cksum_fail(list(_rx_buf[0:total]))
        del _rx_buf[0]
        return True
    payload = bytes(_rx_buf[3:3 + plen])
    del _rx_buf[0:total]
    _dispatch(func, payload)
    return True


def _dispatch(func, payload):
    global _pending_cmd, _trace_inflight_ms
    if func == CMD_READ_ENCODER:
        raw = _read_int48(payload)
        # Trace polls only go out while no _ENC_LABELS read is pending, so an
        # outstanding trace poll is always OLDER than a labeled read: replies come
        # back in request order, the trace one first.
        if _trace_inflight_ms:
            _trace_inflight_ms = 0
            _on_trace_read(raw)
            return
        if _pending_cmd in _ENC_LABELS:
            label = _pending_cmd
            _pending_cmd = None
            if label == "sync_read":
                _on_sync_read(raw)
            elif label == "move_sync":
                pos = _ground_ok(raw, motor_pos_steps + move_in_flight_delta, "move_sync")
                if pos is not None:
                    _emit_moved(pos, 0)
                    _finalize_move_to(pos)
            elif label == "nudge_sync":
                pos = _ground_ok(raw, motor_pos_steps + _nudge_delta, "nudge_sync")
                if pos is not None:
                    _emit_moved(pos, 0)
                    _finalize_nudge_to(pos)
            elif label == "stream_seed":
                pos = _ground_ok(raw, motor_pos_steps, "stream_seed")
                if pos is not None:
                    _on_stream_seed(pos)
            elif label == "stall_sync":
                _on_stall_sync(raw)
            elif label == "stream_end_read":
                _on_stream_end_read(raw)
            elif label == "home_seed":
                _home_begin_drive(raw)
            elif label == "home_contact_read":
                _on_home_contact_read(raw)
            elif label == "home_backoff_read":
                _home_verify_backoff(raw)
            elif label == "home_zero_seed":
                _on_home_zero_seed(raw)
            elif label == "breath_read":
                _on_breath_read(raw)
        elif _trace_interval_ms:
            _on_trace_read(raw)
        return
    status = payload[0] if payload else 0
    if func == CMD_MOVE_POS:
        _on_move_reply(status)
    elif func == CMD_READ_SHAFT_PROT:
        _on_prot_read(status)
    elif func == CMD_RELEASE_PROT and _pending_cmd == "home_release":
        _pending_cmd = None
        _send_and_expect(bytes([CMD_READ_ENCODER]), "home_seed")
    elif func == CMD_STOP and _pending_cmd in ("stop", "breath_stop", "breath_stop_idle"):
        label = _pending_cmd
        _pending_cmd = None
        if label == "breath_stop":
            _on_breath_stop_jump()
        elif label == "breath_stop_idle":
            _send(bytes([CMD_ENABLE, 0x00]))
            _set_state_idle()
    # all other config/motion-status replies (ENABLE, F6, SET_ZERO, init acks) are
    # fire-and-forget here -- init verifies its own blockingly.


def _on_move_reply(status):
    """0xFD reply. status: 1 starting, 2 complete, 3 limit-stop, 0 fail/stall."""
    global _pending_cmd, _motion_ctx, state, last_error, move_in_flight_delta, _breath_enabled
    global _nudge_delta
    ctx = _motion_ctx
    if status == 1:
        _cmd_started()
        return
    if ctx == "home_drive":
        if status == 0:                    # stall = seat contact (expected)
            _pending_cmd = None
            _home_on_contact()
        elif status == 2:                  # traveled full seek with no stall -> no seat found
            _pending_cmd = None
            _send(bytes([CMD_ENABLE, 0x00]))
            state = "error"
            last_error = "home_no_contact"
            _motion_ctx = None
            print("Valve: homing drove full range with no stall -- no seat, disabled")
        return
    if ctx == "home_backoff":
        _pending_cmd = None
        _motion_ctx = None
        if status == 2:
            _send_and_expect(bytes([CMD_READ_ENCODER]), "home_backoff_read")
        elif status == 0:
            _send(bytes([CMD_ENABLE, 0x00]))
            state = "error"
            last_error = "home_backoff_stalled"
            print("Valve: backoff stalled -- disabled")
        return
    if ctx == "nudge":
        if status == 2:
            _pending_cmd = None
            _motion_ctx = None
            _move_meta_done()
            _send_and_expect(bytes([CMD_READ_ENCODER]), "nudge_sync")
        elif status == 0:
            _pending_cmd = None
            _motion_ctx = None
            _move_meta_done()
            _nudge_delta = 0
            _send(bytes([CMD_RELEASE_PROT]))    # clear the lockrotor latch
            _send(bytes([CMD_ENABLE, 0x00]))
            state = "idle"
            print("Valve: nudge stalled -- hit a stop, de-energized")
            _send_and_expect(bytes([CMD_READ_ENCODER]), "stall_sync")
        return
    # normal move
    if status == 2:
        _pending_cmd = None
        _motion_ctx = None
        _move_meta_done()
        _send_and_expect(bytes([CMD_READ_ENCODER]), "move_sync")
    elif status == 0:
        _pending_cmd = None
        _motion_ctx = None
        _move_meta_done()
        state = "stalled"
        last_error = "move_stalled"
        move_in_flight_delta = 0
        _breath_enabled = False
        _send(bytes([CMD_STOP]))
        _send(bytes([CMD_RELEASE_PROT]))
        _send(bytes([CMD_ENABLE, 0x00]))
        print("Valve: move stalled (status=0) -- motor disabled")
        _send_and_expect(bytes([CMD_READ_ENCODER]), "stall_sync")


def _cmd_started():
    global _pending_cmd, _cmd_sent_ms
    _pending_cmd = "move_done"
    _cmd_sent_ms = supervisor.ticks_ms()


def _on_prot_read(status):
    """0x3E reply: the homing-timeout backstop check, or the stream/breath stall watch."""
    global _pending_cmd
    if _pending_cmd == "home_prot_check":
        _pending_cmd = None
        if state == "homing" and status == 1:
            _home_on_contact()
        elif state == "homing":
            print("Valve: home timeout, no stall latched -- aborting")
            _send(bytes([CMD_ENABLE, 0x00]))
            _set_error("home_timeout")
        return
    if status == 1 and state in ("streaming", "breathing"):
        _follow_stalled()


def _set_error(err):
    global state, last_error
    state = "error"
    last_error = err


def _check_mks_silence(now):
    global state, last_error, _breath_enabled, _breath_last_rpm
    if state != "breathing" or _breath_last_good_read_ms == 0:
        return
    now = supervisor.ticks_ms()
    limit = max(MKS_SILENCE_LIMIT_MS, 3 * _breath_period_ms)
    if _ticks_diff(now, _breath_last_good_read_ms) < limit:
        return
    _send(bytes([CMD_CONSTANT_SPEED, 0x00, 0x00, 0x00]))
    _send(bytes([CMD_STOP]))
    _send(bytes([CMD_ENABLE, 0x00]))
    _breath_enabled = False
    _breath_last_rpm = None
    state = "stalled"
    last_error = "mks_silent"
    print("Valve: no valley read in 3 breaths -- lost MKS, disabled")


def _check_timeout():
    global _pending_cmd, state, last_error, _breath_enabled, _breath_last_rpm, _motion_ctx
    if _pending_cmd is None:
        return
    label = _pending_cmd
    if label == "move_done":
        timeout = _move_timeout_ms          # rpm-aware, set when the move was staged
    else:
        timeout = CMD_TIMEOUT_MS
    if _ticks_diff(supervisor.ticks_ms(), _cmd_sent_ms) < timeout:
        return
    print(f"Valve: UART timeout waiting for {label} (ctx={_motion_ctx})")
    if label in ("breath", "breath_read"):
        _pending_cmd = None
        return
    if label == "sync_read":
        _pending_cmd = None
        _dispatch_pending_target()
        return
    if label == "move_sync":
        _pending_cmd = None
        _emit_moved(None, 0)
        _finalize_move_to(motor_pos_steps + move_in_flight_delta)
        return
    if label == "nudge_sync":
        _pending_cmd = None
        _emit_moved(None, 0)
        _finalize_nudge_to(motor_pos_steps + _nudge_delta)
        return
    if label == "stall_sync":
        _pending_cmd = None
        _emit_moved(None, 1)
        return
    if label == "stream_end_read":
        _pending_cmd = None
        _on_stream_end_read(None)
        return
    if label in ("home_seed", "home_contact_read", "home_backoff_read", "home_zero_seed"):
        print(f"Valve: re-reading encoder after {label} timeout")
        _send_and_expect(bytes([CMD_READ_ENCODER]), label)
        return
    if label == "home_release":
        _pending_cmd = None
        _send_and_expect(bytes([CMD_READ_ENCODER]), "home_seed")
        return
    # generic: cut motion + de-energize
    _send(bytes([CMD_CONSTANT_SPEED, 0x00, 0x00, 0x00]))
    _send(bytes([CMD_STOP]))
    _send(bytes([CMD_ENABLE, 0x00]))
    _breath_enabled = False
    _breath_last_rpm = None
    _motion_ctx = None
    _emit_moved(None, 1)
    last_error = f"timeout_{label}"
    _pending_cmd = None
    if state != "stalled":
        state = "error"


# ── MQTT inbound ─────────────────────────────────────────────────────────────

def handle_mqtt(topic, payload):
    global pending_target, open_steps
    if topic == TOPIC_VALVE_TARGET:
        val = _parse_float(payload)
        if val is None:
            print(f"Valve: bad target payload: {payload}")
            return
        pending_target = max(0.0, min(1.0, val))
    elif topic == TOPIC_VALVE_HOME:
        hp = payload.encode("utf-8") if isinstance(payload, str) else bytes(payload or b"")
        if hp.strip() == b"here":
            cmd_home_here()
        else:
            cmd_home()
    elif topic == TOPIC_VALVE_STOP:
        cmd_stop()
    elif topic == TOPIC_VALVE_CALIBRATE:
        steps = _parse_int(payload)
        if steps is None or not (100 <= steps <= 1000000):
            print(f"Valve: bad calibrate payload: {payload}")
            return
        open_steps = steps
        print(f"Valve: open_steps = {open_steps}")
    elif topic == TOPIC_VALVE_BREATH:
        _handle_breath_payload(payload)
    elif topic == TOPIC_VALVE_MAXTORQUE:
        ma = _parse_int(payload)
        if ma is None or not (0 <= ma <= 3000):
            print(f"Valve: bad current payload: {payload}")
            return
        _set_current(ma)
    elif topic == TOPIC_VALVE_NUDGE:
        _cmd_nudge(_parse_int(payload))
    elif topic == TOPIC_VALVE_LIMITS:
        _handle_limits_payload(payload)
    elif topic == TOPIC_VALVE_TRACE:
        _handle_trace_payload(payload)


def _set_current(ma):
    global _current_ma
    ma = max(0, min(3000, int(ma)))
    _current_ma = ma
    _send(bytes([CMD_SET_CURRENT, (ma >> 8) & 0xFF, ma & 0xFF]))
    print(f"Valve: set current {ma} mA")


def _handle_breath_payload(payload):
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
        was = _breath_enabled
        _breath_enabled = bool(data["enabled"])
        if was and not _breath_enabled and state == "breathing":
            _exit_breath_to_idle()
    print(f"Valve: breath A={_breath_amplitude:.3f} T={_breath_period_ms}ms skew={_breath_skew:.2f} en={_breath_enabled}")


def _handle_limits_payload(payload):
    """Runtime motion limits (in-memory). Empty payload = query. Homing is not settable."""
    global move_rpm, move_acc, stream_max_rpm, breath_max_rpm
    if isinstance(payload, (bytes, bytearray)):
        try:
            payload = payload.decode("utf-8")
        except UnicodeError:
            return
    payload = (payload or "").strip()
    if payload:
        try:
            data = json.loads(payload)
        except (ValueError, TypeError):
            data = None
        if not isinstance(data, dict):
            print(f"Valve: bad limits payload: {payload}")
            data = {}            # still ack below -- the tool waits on limits_ack
        if "move_rpm" in data:
            try:
                move_rpm = max(1, min(3000, int(data["move_rpm"])))
            except (ValueError, TypeError):
                pass
        if "move_acc" in data:
            try:
                move_acc = max(0, min(255, int(data["move_acc"])))
            except (ValueError, TypeError):
                pass
        if "stream_max_rpm" in data:
            try:
                stream_max_rpm = max(1, min(3000, int(data["stream_max_rpm"])))
            except (ValueError, TypeError):
                pass
        if "breath_max_rpm" in data:
            try:
                breath_max_rpm = max(1, min(3000, int(data["breath_max_rpm"])))
            except (ValueError, TypeError):
                pass
        print(f"Valve: limits move={move_rpm}rpm acc={move_acc} stream<={stream_max_rpm} breath<={breath_max_rpm}")
    # ack on a distinct topic -- echoing the inbound one would loop through the bridge
    _queue_out((TOPIC_VALVE_LIMITS_ACK, json.dumps({
        "move_rpm": move_rpm, "move_acc": move_acc,
        "stream_max_rpm": stream_max_rpm, "breath_max_rpm": breath_max_rpm})))


def _handle_trace_payload(payload):
    global _trace_interval_ms, _trace_last_ms, _trace_inflight_ms
    iv = _parse_int(payload)
    if iv is None:
        print(f"Valve: bad trace payload: {payload}")
        return
    _trace_interval_ms = 0 if iv <= 0 else max(20, min(1000, iv))
    _trace_last_ms = 0
    _trace_inflight_ms = 0
    print(f"Valve: trace {_trace_interval_ms}ms")


def _on_trace_read(raw):
    # unclamped: divergence outside [0, open_steps] is signal
    pos = _encoder_pos_steps(raw)
    if state == "streaming" and (pos < -STREAM_GUARD_MARGIN
                                 or pos > open_steps + STREAM_GUARD_MARGIN):
        _stream_runaway(pos)
        return
    if _trace_interval_ms:          # guard polls don't spam tracept unless asked
        _queue_out((TOPIC_VALVE_TRACEPT,
                    "%d %d" % (supervisor.ticks_ms(), pos)))


def _stream_runaway(pos):
    """The physical shaft left the window mid-stream. Grounding is still valid --
    this is a control failure (resonant overshoot / rectified walk), not a zero loss."""
    global state, last_error, _breath_last_rpm, _stream_max_idx, _stream_played
    _send(bytes([CMD_CONSTANT_SPEED, 0x00, 0x00, 0x00]))   # snap halt
    _send(bytes([CMD_STOP]))
    _send(bytes([CMD_ENABLE, 0x00]))
    _breath_last_rpm = None
    state = "stalled"
    last_error = "stream_runaway"
    _stream_max_idx = -1
    _stream_played = -1
    _schedule_stream_end_read()
    print("Valve: stream RUNAWAY -- shaft %d, window 0..%d (+/-%d); stopped"
          % (pos, open_steps, STREAM_GUARD_MARGIN))


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


def _cmd_nudge(deg):
    """Debug: raw relative move of `deg` degrees, no soft-limit clamp.
    +deg = toward closed, -deg = toward open."""
    global state, _nudge_delta, _motion_ctx, _pending_move, _move_settle_at
    global _move_timeout_ms, _move_meta
    if deg is None:
        print("Valve: bad nudge payload")
        return
    if state not in ("idle", "unknown") or _pending_cmd is not None:
        print(f"Valve: refusing nudge -- state={state} pending={_pending_cmd}")
        return
    deg = max(-360, min(360, deg))
    steps = abs(deg) * STEPS_PER_REV // 360
    if steps == 0:
        return
    direction = DIR_TOWARD_CLOSED if deg > 0 else DIR_TOWARD_OPEN
    _nudge_delta = -steps if deg > 0 else steps   # open-positive, like every other delta
    state = "nudging"
    _motion_ctx = "nudge"
    _cancel_stream_end_read()
    print(f"Valve: nudge {deg} deg ({steps} steps)")
    _move_meta = {"cmd": _nudge_delta, "pre": motor_pos_steps, "rpm": move_rpm,
                  "acc": move_acc, "fired": 0, "ms": 0}   # pre may be stale unhomed
    _move_timeout_ms = _calc_move_timeout(steps, move_rpm, move_acc)
    _pending_move = _speed_body(CMD_MOVE_POS, direction, move_rpm, move_acc, steps)
    _send(bytes([CMD_ENABLE, 0x01]))
    _move_settle_at = (supervisor.ticks_ms() + MOVE_SETTLE_MS) & 0x3FFFFFFF


# ── Init ─────────────────────────────────────────────────────────────────────

def _blocking_drain():
    _drain_can()


def _blocking_wait_status(timeout_ms=500):
    """Block until a reply CAN frame arrives; return its status byte, or None on timeout."""
    lis = _ensure_listener()
    if lis is None:
        return None
    deadline = (supervisor.ticks_ms() + timeout_ms) & 0x3FFFFFFF
    while True:
        if _ticks_diff(supervisor.ticks_ms(), deadline) < 0x1FFFFFFF:
            return None
        try:
            msg = lis.receive()
        except Exception:
            msg = None
        if msg is not None:
            d = bytes(getattr(msg, "data", b"") or b"")
            if len(d) >= 3 and (msg.id + sum(d[:-1])) & 0xFF == d[-1]:
                return d[1]
        time.sleep(0.002)


def _blocking_setup(body, timeout_ms=600):
    _blocking_drain()
    _send(body)
    return _blocking_wait_status(timeout_ms)


def _blocking_read_encoder(timeout_ms=600):
    """Blocking 0x31 read (motor stopped). Returns int48 raw, or None on timeout."""
    _drain_can()
    _send(bytes([CMD_READ_ENCODER]))
    lis = _ensure_listener()
    if lis is None:
        return None
    deadline = (supervisor.ticks_ms() + timeout_ms) & 0x3FFFFFFF
    while True:
        if _ticks_diff(supervisor.ticks_ms(), deadline) < 0x1FFFFFFF:
            return None
        try:
            msg = lis.receive()
        except Exception:
            msg = None
        if msg is not None:
            d = bytes(getattr(msg, "data", b"") or b"")
            if (len(d) == 8 and d[0] == CMD_READ_ENCODER
                    and (msg.id + sum(d[:-1])) & 0xFF == d[-1]):
                return _read_int48(d[1:7])
        time.sleep(0.002)


def _blocking_read_status(func, timeout_ms=200):
    """Blocking single-byte status read (e.g. 0x3E shaft-protect, 0xF1 run-status, 0x3A
    enable). Sends [func], waits for the reply [func, status, crc]; returns status or None."""
    _drain_can()
    _send(bytes([func]))
    lis = _ensure_listener()
    if lis is None:
        return None
    deadline = (supervisor.ticks_ms() + timeout_ms) & 0x3FFFFFFF
    while True:
        if _ticks_diff(supervisor.ticks_ms(), deadline) < 0x1FFFFFFF:
            return None
        try:
            msg = lis.receive()
        except Exception:
            msg = None
        if msg is not None:
            d = bytes(getattr(msg, "data", b"") or b"")
            if len(d) >= 3 and d[0] == func and (msg.id + sum(d[:-1])) & 0xFF == d[-1]:
                return d[1]
        time.sleep(0.002)


def init():
    """Configure the 42D for serial closed-loop control. Blocking; runs once at boot."""
    global state, last_error
    print("Valve(42D): init over CAN")
    _led_init()
    time.sleep(0.2)
    _blocking_drain()
    # Ensure responses on + active (two-stage motion replies status=1->2). 42D default,
    # but assert it -- sent bare so an unexpected reply can't fail init.
    _send(bytes([CMD_SET_RESPOND, 0x01, 0x01]))
    time.sleep(0.05)
    _blocking_drain()
    for attempt in range(2):
        ok = (_blocking_setup(bytes([CMD_SET_WORKMODE, WORKMODE_SR_VFOC])) == 1
              and _blocking_setup(bytes([CMD_SET_MICROSTEP, MICROSTEP & 0xFF])) == 1
              and _blocking_setup(bytes([CMD_SET_CURRENT, (VALVE_CURRENT_MA >> 8) & 0xFF,
                                         VALVE_CURRENT_MA & 0xFF])) == 1
              and _blocking_setup(bytes([CMD_SET_PROTECT, 0x01])) == 1   # ON: seat detector + jam net
              and _blocking_setup(bytes([CMD_ENABLE, 0x01])) == 1)
        if ok:
            _send(bytes([CMD_STOP]))
            _send(bytes([CMD_ENABLE, 0x00]))   # de-energize at idle; valve self-holds
            if HOMING_DISABLED:
                _fake_home_at_zero()           # boot position = 0, ready immediately
                print("Valve(42D): init OK -- homing disabled, boot pos = 0, ready")
            else:
                print("Valve(42D): init OK (de-energized) -- must home before moves")
                state = "unknown"
            return
        print("Valve(42D): init attempt", attempt, "-- setup ACK failed")
        time.sleep(0.2)
    print("Valve(42D): init FAILED -- check CAN wiring/termination, motor CAN ID, "
          "bitrate 500k, crystal_freq, SR_vFOC")
    state = "error"
    last_error = "init_setup_failed"


# ── Breath oscillator ────────────────────────────────────────────────────────

def _breath_phase_and_dphase(now):
    period_ms = max(100, _breath_period_ms)
    s = max(0.05, min(0.95, _breath_skew))
    t_in_cycle = _ticks_diff(now, _breath_phase_start_ms) % period_ms
    rise_ms = int(s * period_ms)
    if t_in_cycle < rise_ms:
        phase = -math.pi / 2 + math.pi * t_in_cycle / rise_ms
        dphase = math.pi / (rise_ms / 1000.0)
    else:
        fall_ms = period_ms - rise_ms
        phase = math.pi / 2 + math.pi * (t_in_cycle - rise_ms) / fall_ms
        dphase = math.pi / (fall_ms / 1000.0)
    return phase, dphase


def _breath_rpm_signed(now):
    """Signed RPM for the breath at `now`: + = toward open. None at the zero-RPM rest."""
    phase, dphase = _breath_phase_and_dphase(now)
    osc_frac_per_sec = _breath_amplitude * math.cos(phase) * dphase
    if open_steps > 0 and BREATH_DRIFT_TAU_S > 0:
        drift = (target_pos_steps - motor_pos_steps) / open_steps / BREATH_DRIFT_TAU_S
    else:
        drift = 0.0
    velocity_sps = (osc_frac_per_sec + drift) * open_steps
    rpm = int(round(_steps_to_rpm(velocity_sps)))
    if rpm == 0:
        return None
    rpm = min(rpm, breath_max_rpm)
    toward_open = velocity_sps > 0
    if toward_open and motor_pos_steps >= open_steps:
        return None
    if not toward_open and motor_pos_steps <= 0:
        return None
    return rpm if toward_open else -rpm


def _integrate_breath_motion(now):
    global motor_pos_steps
    if _breath_last_rpm is None or _breath_last_update_ms == 0:
        return
    elapsed_ms = _ticks_diff(now, _breath_last_update_ms)
    steps = abs(_breath_last_rpm) * STEPS_PER_REV * elapsed_ms // 60000
    if _breath_last_rpm > 0:
        motor_pos_steps += steps
    else:
        motor_pos_steps -= steps


def _enter_breathing(now):
    global state, _breath_phase_start_ms, _breath_last_update_ms, _breath_last_rpm
    global _breath_last_good_read_ms, _breath_prev_t, _breath_at_valley
    _send(bytes([CMD_ENABLE, 0x01]))
    state = "breathing"
    _breath_phase_start_ms = now
    _breath_last_update_ms = 0
    _breath_last_rpm = None
    _breath_prev_t = 0
    _breath_at_valley = False
    _breath_last_good_read_ms = now
    print(f"Valve: breathing -- baseline={target_pos_steps} A={_breath_amplitude:.3f} T={_breath_period_ms}ms")


def _service_breath(now):
    global _breath_last_update_ms, _breath_last_rpm, _breath_prev_t, _breath_at_valley
    if _pending_cmd is not None:
        return
    if _ticks_diff(now, _breath_last_update_ms) < BREATH_UPDATE_MS:
        return
    _integrate_breath_motion(now)
    _breath_last_update_ms = now
    period_ms = max(100, _breath_period_ms)
    t = _ticks_diff(now, _breath_phase_start_ms) % period_ms
    crossed_valley = t < _breath_prev_t
    _breath_prev_t = t
    if crossed_valley:
        # ground mid-motion (0x31 replies while moving); halting here stole rise
        # time every cycle -- the phase clock keeps running -- and rectified into
        # a downward walk at fast periods (bench 2026-06-12)
        _send_and_expect(bytes([CMD_READ_ENCODER]), "breath_read")
        return
    rpm = _breath_rpm_signed(now)
    if rpm != _breath_last_rpm:
        if rpm is None:
            _send(bytes([CMD_CONSTANT_SPEED, 0x00, 0x00, BREATH_ACC]))
        else:
            direction = DIR_TOWARD_OPEN if rpm > 0 else DIR_TOWARD_CLOSED
            _send(_speed_body(CMD_CONSTANT_SPEED, direction, abs(rpm), BREATH_ACC))
        _breath_last_rpm = rpm


def _on_breath_read(raw):
    global motor_pos_steps, _breath_last_good_read_ms
    pos = _ground_ok(raw, motor_pos_steps, "breath_read")
    if pos is None:
        return                      # ground lost -- breathing already disabled
    motor_pos_steps = pos
    _breath_last_good_read_ms = supervisor.ticks_ms()


def _exit_breath_for_jump(new_step_target):
    global state, _pending_jump_target
    _pending_jump_target = new_step_target
    state = "exiting_breath"
    _send_and_expect(bytes([CMD_STOP]), "breath_stop")


def _exit_breath_to_idle():
    global state
    state = "exiting_breath"
    _send_and_expect(bytes([CMD_STOP]), "breath_stop_idle")


def _set_state_idle():
    global state
    state = "idle"


def _on_breath_stop_jump():
    """STOP ack after a big breath baseline jump: go idle and issue the queued move."""
    global state, _pending_jump_target
    state = "idle"
    target = _pending_jump_target
    _pending_jump_target = 0
    _issue_move(target)


# ── Streamed waveform playback (open-loop; the host clock is the authority) ────

def handle_stream(ftype, payload):
    if ftype == SF_START:
        _stream_begin(payload)
    elif ftype == SF_SAMPLES:
        _stream_add(payload)
    elif ftype == SF_STOP:
        _stream_end()
    elif ftype == SF_PING:
        _stream_pong(payload)


def _stream_begin(payload):
    global state, _stream_rate, _stream_base_ms, _stream_epoch
    global _stream_max_idx, _stream_played, _breath_last_rpm, pending_target
    if len(payload) < 6:
        return
    _stream_rate = max(1, (payload[0] << 8) | payload[1])
    _stream_base_ms = int.from_bytes(payload[2:6], "big")
    _stream_epoch = supervisor.ticks_ms()
    _stream_max_idx = -1
    _stream_played = -1
    pending_target = None
    _breath_last_rpm = None
    _send(bytes([CMD_ENABLE, 0x01]))   # energize for continuous motion
    state = "streaming"
    # seed read: verifies grounding (42D brownout = ground_lost, no playback) and
    # re-grounds bookkeeping so the stream starts encoder-true. _service_stream
    # holds samples while this read is pending; a reply timeout error-stops the stream.
    _send_and_expect(bytes([CMD_READ_ENCODER]), "stream_seed")
    print("Valve: streaming @ %dHz base=%dms%s"
          % (_stream_rate, _stream_base_ms, "" if homed else " (open-loop, NOT homed)"))


def _on_stream_seed(pos):
    global motor_pos_steps
    motor_pos_steps = pos


def _stream_add(payload):
    global _stream_max_idx
    if len(payload) < 6:
        return
    start = int.from_bytes(payload[0:4], "big")
    count = (payload[4] << 8) | payload[5]
    pos = payload[6:6 + count]
    for k in range(len(pos)):
        idx = start + k
        _stream_buf[idx % STREAM_CAP] = pos[k]
        if idx > _stream_max_idx:
            _stream_max_idx = idx


def _stream_end():
    global state, _stream_max_idx, _stream_played, _breath_last_rpm
    # acc=0 snap stop: a ramped hold (acc=8 = ~7 s decel from 600 rpm) would smear
    # the streamend ground-truth read
    _send(bytes([CMD_CONSTANT_SPEED, 0x00, 0x00, 0x00]))
    _breath_last_rpm = None
    _stream_max_idx = -1
    _stream_played = -1
    if state == "streaming":
        _schedule_stream_end_read()
        state = "idle"
    print("Valve: stream stop")


def _follow_stalled():
    """Lockrotor latched mid-stream/breath (0xF6 replies are fire-and-forget, so the motor
    stopped silently while dead-reckoning kept advancing). Cut everything, measure the drift."""
    global state, last_error, _breath_enabled, _breath_last_rpm, _stream_max_idx, _stream_played
    was = state
    _send(bytes([CMD_STOP]))
    _send(bytes([CMD_RELEASE_PROT]))
    _send(bytes([CMD_ENABLE, 0x00]))
    _breath_enabled = False
    _breath_last_rpm = None
    state = "stalled"
    last_error = "stream_stalled" if was == "streaming" else "breath_stalled"
    if was == "streaming":
        _stream_max_idx = -1
        _stream_played = -1
        _schedule_stream_end_read()
    print("Valve: %s -- lockrotor latched, disabled" % last_error)


def _schedule_stream_end_read():
    global _stream_cmd_frac, _stream_end_read_at
    _stream_cmd_frac = _pos_fraction()
    _stream_end_read_at = ((supervisor.ticks_ms() + STREAM_END_SETTLE_MS) & 0x3FFFFFFF) or 1


def _on_stream_end_read(raw):
    """Encoder ground truth after an open-loop stream: report divergence, re-ground."""
    global motor_pos_steps, target_pos_steps
    if raw is None:
        _queue_out((TOPIC_VALVE_STREAMEND, json.dumps(
            {"cmd": round(_stream_cmd_frac, 3), "enc": None, "err_steps": None})))
        return
    pos = _encoder_pos_steps(raw)
    cmd_steps = int(round(_stream_cmd_frac * open_steps))
    _queue_out((TOPIC_VALVE_STREAMEND, json.dumps({
        "cmd": round(_stream_cmd_frac, 3),
        "enc": round(pos / open_steps, 3) if open_steps > 0 else 0.0,
        "err_steps": pos - cmd_steps})))
    motor_pos_steps = pos
    target_pos_steps = pos


def _stream_pong(payload):
    token = ((payload[0] << 8) | payload[1]) if len(payload) >= 2 else 0
    _queue_out((TOPIC_VALVE_PONG, "%d %d" % (token, supervisor.ticks_ms())))


def _service_stream(now):
    """Drive the valve toward the sample due at the current playback time, on our
    own clock. Velocity-follow via bare 0xF6 (acc=0 -> snap), open-loop dead-reckon."""
    global _stream_played, _breath_last_rpm, motor_pos_steps, _stream_last_telem_ms
    if _pending_cmd is not None:
        return
    cur_play = _stream_base_ms + _ticks_diff(now, _stream_epoch)
    if cur_play < 0:
        return
    idx = (cur_play * _stream_rate) // 1000
    if idx <= _stream_played:
        return
    if idx > _stream_max_idx:                       # underrun / end -> hold
        if _breath_last_rpm not in (None, 0):
            _send(bytes([CMD_CONSTANT_SPEED, 0x00, 0x00, 0x00]))  # snap: acc-8 coasts ~revs
            _breath_last_rpm = None
        return
    oldest = _stream_max_idx - STREAM_CAP + 1        # don't reach past the ring
    if idx < oldest:
        idx = oldest
    target_step = _stream_buf[idx % STREAM_CAP] * open_steps // 255
    if target_step < 0:
        target_step = 0
    elif target_step > open_steps:
        target_step = open_steps
    dt_s = (idx - _stream_played) / _stream_rate if _stream_played >= 0 else 1.0 / _stream_rate
    delta = target_step - motor_pos_steps
    rpm = int(round(_steps_to_rpm(delta / dt_s))) if dt_s > 0 else 0
    clamped = rpm > stream_max_rpm
    if clamped:
        rpm = stream_max_rpm
    if rpm == 0:
        if _breath_last_rpm not in (None, 0):
            _send(bytes([CMD_CONSTANT_SPEED, 0x00, 0x00, 0x00]))  # snap: acc-8 coasts ~revs
            _breath_last_rpm = None
    else:
        direction = DIR_TOWARD_OPEN if delta > 0 else DIR_TOWARD_CLOSED
        _send(_speed_body(CMD_CONSTANT_SPEED, direction, rpm, 0))   # acc=0: snap
        _breath_last_rpm = rpm if delta > 0 else -rpm
    if clamped:
        # bookkeep only what the clamped speed covers this tick, so later samples
        # keep commanding catch-up; teleporting here = the 2026-06-11 seat ram
        adv = int(rpm * STEPS_PER_REV * dt_s / 60.0)
        motor_pos_steps += adv if delta > 0 else -adv
    else:
        motor_pos_steps = target_step               # open-loop: commanded IS position
    _stream_played = idx
    if _ticks_diff(now, _stream_last_telem_ms) >= STREAM_TELEM_MS:
        _stream_last_telem_ms = now
        _queue_out((TOPIC_VALVE_STREAMPOS, "%d %.3f" % (cur_play, _pos_fraction())))


# ── RGB status LED ────────────────────────────────────────────────────────────

def _led_init():
    global _led, _led_mode
    try:
        import pwmio
        _led = (pwmio.PWMOut(board.LED_RED, frequency=1000, duty_cycle=65535),
                pwmio.PWMOut(board.LED_GREEN, frequency=1000, duty_cycle=65535),
                pwmio.PWMOut(board.LED_BLUE, frequency=1000, duty_cycle=65535))
        _led_mode = "rgb"
    except Exception:
        try:
            import digitalio
            d = digitalio.DigitalInOut(board.LED)   # Pico W: one LED on the cyw43, digital only
            d.direction = digitalio.Direction.OUTPUT
            _led = (d,)
            _led_mode = "digital"
        except Exception as e:
            print("Valve: status LED unavailable:", e)
            _led = None
            _led_mode = None


def _update_led(now):
    """Show actuation level on the status LED. RGB (XIAO): faint-blue pilot when closed
    -> red/orange as it opens (active-low). Single LED (Pico W): coarse on when open."""
    global _led_last_ms
    if _led is None or _ticks_diff(now, _led_last_ms) < LED_UPDATE_MS:
        return
    _led_last_ms = now
    f = _pos_fraction()
    f = 0.0 if f < 0.0 else 1.0 if f > 1.0 else f
    if _led_mode == "rgb":
        r, g, b = f, f * f * 0.5, (1.0 - f) * 0.2
        _led[0].duty_cycle = 65535 - int(r * 65535)
        _led[1].duty_cycle = 65535 - int(g * 65535)
        _led[2].duty_cycle = 65535 - int(b * 65535)
    else:
        _led[0].value = f > 0.10


# ── Service loop ─────────────────────────────────────────────────────────────

def service():
    global pending_target, last_target_ms, state, last_error
    global target_pos_steps, _pending_move, _pending_sync_target, _pending_jump_target
    global _trace_last_ms, _trace_inflight_ms, _prot_poll_last_ms, _stream_end_read_at
    global _guard_last_ms

    now = supervisor.ticks_ms()
    _poll_can()
    _check_timeout()
    _check_mks_silence(now)

    # Fire a staged move once its post-ENABLE settle elapsed.
    if (_pending_move is not None and _pending_cmd is None
            and _ticks_diff(now, _move_settle_at) < 0x1FFFFFFF):
        mv = _pending_move
        _pending_move = None
        _send_and_expect(mv, "move_start")
        if _move_meta is not None:
            _move_meta["fired"] = now

    # In-flight encoder trace: bare 0x31 polls (replies never clobber a pending wait).
    if _trace_interval_ms:
        if _trace_inflight_ms and _ticks_diff(now, _trace_inflight_ms) > TRACE_STALE_MS:
            _trace_inflight_ms = 0                      # reply lost; don't wedge polling
        if (state in ("moving", "streaming") and not _trace_inflight_ms
                and _pending_cmd not in _ENC_LABELS
                and _ticks_diff(now, _trace_last_ms) >= _trace_interval_ms):
            _trace_last_ms = now
            _trace_inflight_ms = now or 1
            _send(bytes([CMD_READ_ENCODER]))

    # Runaway guard polls: always-on while streaming, rides the trace reply path.
    if state == "streaming":
        if _trace_inflight_ms and _ticks_diff(now, _trace_inflight_ms) > TRACE_STALE_MS:
            _trace_inflight_ms = 0
        if (not _trace_inflight_ms and _pending_cmd not in _ENC_LABELS
                and _ticks_diff(now, _guard_last_ms) >= STREAM_GUARD_MS):
            _guard_last_ms = now
            _trace_inflight_ms = now or 1
            _send(bytes([CMD_READ_ENCODER]))

    # Stall watch for the silent 0xF6 follow modes.
    if (state in ("streaming", "breathing")
            and _ticks_diff(now, _prot_poll_last_ms) >= PROT_POLL_MS):
        _prot_poll_last_ms = now
        _send(bytes([CMD_READ_SHAFT_PROT]))

    # Post-stream ground-truth read once the snap-stop settled.
    if (_stream_end_read_at and _pending_cmd is None
            and _ticks_diff(now, _stream_end_read_at) < 0x1FFFFFFF):
        _stream_end_read_at = 0
        _send_and_expect(bytes([CMD_READ_ENCODER]), "stream_end_read")

    if state == "homing":
        # final backstop only -- the seek's own move_done timeout checks 0x3E first.
        if _ticks_diff(now, _home_started_ms) >= HOME_TIMEOUT_MS + 20000:
            print("Valve: homing wedged -- disabled")
            cmd_stop()
            _send(bytes([CMD_ENABLE, 0x00]))
            _set_error("home_timeout")
    elif state == "homing_finalize":
        _service_finalize(now)
    elif state == "breathing":
        if (pending_target is not None
                and _ticks_diff(now, last_target_ms) >= TARGET_MIN_MS):
            new_target = pending_target
            pending_target = None
            last_target_ms = now
            new_step_target = int(round(new_target * open_steps))
            delta_frac = abs(new_step_target - target_pos_steps) / open_steps if open_steps > 0 else 0
            target_pos_steps = new_step_target
            if delta_frac > BREATH_BIG_JUMP:
                _exit_breath_for_jump(new_step_target)
        _service_breath(now)
    elif state == "streaming":
        _service_stream(now)
    elif (state == "idle" and _pending_cmd is None and pending_target is not None
            and _ticks_diff(now, last_target_ms) >= TARGET_MIN_MS):
        target = pending_target
        pending_target = None
        last_target_ms = now
        _pending_sync_target = int(round(target * open_steps))
        if homed:
            _send_and_expect(bytes([CMD_READ_ENCODER]), "sync_read")
        else:
            _dispatch_pending_target()

    _update_led(now)


# ── MQTT outbound ─────────────────────────────────────────────────────────────

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
    return json.dumps(d)


def get_publish_messages():
    global _last_status_ms, _last_actual_ms
    now = supervisor.ticks_ms()
    msgs = []
    if _stream_out:
        msgs.extend(_stream_out)
        _stream_out[:] = []
    interval = STATUS_MOVE_MS if state in ("moving", "homing", "homing_finalize") else STATUS_IDLE_MS
    if _ticks_diff(now, _last_status_ms) >= interval:
        _last_status_ms = now
        msgs.append((TOPIC_VALVE_STATUS, _status_json()))
    if _ticks_diff(now, _last_actual_ms) >= ACTUAL_MS:
        _last_actual_ms = now
        msgs.append((TOPIC_VALVE_ACTUAL, str(round(_pos_fraction(), 3))))
    return msgs

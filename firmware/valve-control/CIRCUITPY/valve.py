# valve.py -- Motorized needle valve via MKS SERVO42D over UART (D6 TX / D7 RX).
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
# !! Values tagged VERIFY were carried/estimated from the 42C and the 42D manual
#    and MUST be confirmed on the bench (direction sense, current, RPM, accel,
#    stall behaviour). Nothing here has run on a 42D yet.

import board
import busio
import supervisor
import json
import math
import struct
import time

uart = busio.UART(board.TX, board.RX, baudrate=38400, timeout=0.1)

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
OPEN_STEPS      = 2000 * _USTEP            # open extent in microsteps. PLACEHOLDER -- recalibrate.

VALVE_CURRENT_MA = 400       # VERIFY. 42D run current in mA (max 3000). Low = gentle seat
                              # force on a stall-home; raise if it can't move, lower if the
                              # seat contact is too hard.
MOVE_RPM        = 40         # VERIFY. 0xFD/0xF6 cruise speed (RPM; 42D speed IS rpm at >=16x)
MOVE_ACC        = 2          # VERIFY. accel byte: each unit-rpm step takes (256-acc)*50us;
                              # acc=0 is instant (no ramp), small acc = gentle ramp.

# 0xFD/0xF6 direction bit (b7 of the speed_hi byte). 42D manual: 0=CCW, 1=CW.
# Which physical sense opens vs closes the valve is UNKNOWN on this build -- VERIFY
# and swap if a target move drives the wrong way.
DIR_TOWARD_OPEN   = 0x80     # VERIFY
DIR_TOWARD_CLOSED = 0x00     # VERIFY

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

# A 0xFD right after ENABLE doesn't execute reliably; energize, stage, fire after settle.
_pending_move   = None
_move_settle_at = 0
MOVE_SETTLE_MS  = 120

# ── Stallguard homing ───────────────────────────────────────────────────────
# Drive toward the closed seat in one 0xFD at low current with locked-rotor
# protection on. Contact = the 42D stalls: 0xFD returns status=0, or (if it
# latches silently) the move times out and 0x3E reads "protected". Then back off
# a margin toward open, verify free motion, and zero there.
HOME_RPM           = 8 * _USTEP   # VERIFY. slow approach
HOME_ACC           = 2            # VERIFY
HOME_MAX_PULSES    = 6 * STEPS_PER_REV   # VERIFY. bound the seek (~6 rev); must exceed
                                          # full-open->seat travel or it errors as "no contact"
HOME_BACKOFF_STEPS = 200 * _USTEP
HOME_BACKOFF_MIN_FRAC = 0.5
HOME_TIMEOUT_MS    = 60000
_home_seed_raw     = 0
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
BREATH_ACC             = 8       # VERIFY. accel byte for breath 0xF6 -- must reach each 100ms
                                  # speed target within the interval, so not too gentle.
MKS_SILENCE_LIMIT_MS   = 2000
_breath_last_good_read_ms = 0
BREATH_BIG_JUMP        = 0.10
BREATH_ENTER_DEADBAND  = 5 * _USTEP
BREATH_MAX_RPM         = 120 * _USTEP   # VERIFY
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
STREAM_TELEM_MS = 200              # executed-position telemetry cadence

_stream_buf      = bytearray(STREAM_CAP)
_stream_max_idx  = -1
_stream_played   = -1
_stream_rate     = 30
_stream_base_ms  = 0
_stream_epoch    = 0
_stream_last_telem_ms = 0
_stream_out      = []              # queued outbound telemetry (pong/streampos)

# ── RGB status LED (XIAO onboard, active-low) -- shows actuation level ────────
_led          = None
_led_last_ms  = 0
LED_UPDATE_MS = 60


def _ticks_diff(later, earlier):
    return (later - earlier) & 0x3FFFFFFF


def _encoder_pos_steps(raw):
    """Absolute valve position in steps from a 0x31 raw read. 0 = homed margin."""
    return int(round((raw - _enc_zero_raw) * _enc_sign / ENC_PER_STEP))


def _steps_to_rpm(steps_per_sec):
    return abs(steps_per_sec) * 60.0 / STEPS_PER_REV


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
    """body = bytes([func, param0, ...]); frame = FA ADDR <body> <crc>."""
    pkt = bytes([TX_HEAD, ADDR]) + bytes(body)
    pkt = pkt + bytes([_checksum(pkt)])
    uart.write(pkt)


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
        b = b + (int(pulses) & 0xFFFFFFFF).to_bytes(4, "big")
    return b


def _drain_uart_buffer():
    global _rx_buf
    while True:
        avail = uart.in_waiting
        if avail <= 0:
            break
        uart.read(avail)
    _rx_buf = bytearray()


# ── Commands ─────────────────────────────────────────────────────────────────

def cmd_stop():
    global state, target_pos_steps, move_in_flight_delta, homed, pending_target
    global _breath_last_rpm, _pending_move, _motion_ctx
    global _stream_max_idx, _stream_played
    _send_and_expect(bytes([CMD_STOP]), "stop")
    _send(bytes([CMD_ENABLE, 0x00]))
    _stream_max_idx = -1
    _stream_played = -1
    _pending_move = None
    _motion_ctx = None
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
    global _motion_ctx
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
    direction = DIR_TOWARD_OPEN if delta > 0 else DIR_TOWARD_CLOSED
    print(f"Valve: move {motor_pos_steps} -> {step_target} (d={delta})")
    _pending_move = _speed_body(CMD_MOVE_POS, direction, MOVE_RPM, MOVE_ACC, abs(delta))
    _send(bytes([CMD_ENABLE, 0x01]))
    _move_settle_at = (supervisor.ticks_ms() + MOVE_SETTLE_MS) & 0x3FFFFFFF
    state = "moving"
    return True


# ── Encoder-grounded move/nudge finalization ─────────────────────────────────

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
    motor_pos_steps = max(0, min(open_steps, _encoder_pos_steps(raw)))
    _dispatch_pending_target()


def _finalize_move_to(pos):
    global motor_pos_steps, move_in_flight_delta, state
    motor_pos_steps = max(0, min(open_steps, pos))
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


# ── Stallguard homing ────────────────────────────────────────────────────────

def cmd_home():
    """Begin homing. Drive toward the closed seat; the 42D's locked-rotor protection
    trips on contact (0xFD status=0, or a timeout + 0x3E latch). Asynchronous."""
    global state, homed, pending_target, _home_seed_raw, _home_started_ms
    global _breath_last_rpm, _pending_move, _motion_ctx
    if state == "breathing" or _breath_last_rpm is not None:
        _send(bytes([CMD_STOP]))
        _breath_last_rpm = None
    pending_target = None
    homed = False
    state = "homing"
    _pending_move = None
    _motion_ctx = None
    _drain_uart_buffer()
    _home_started_ms = supervisor.ticks_ms()
    print("Valve: homing -- clear latch, enable, drive into seat")
    _send_and_expect(bytes([CMD_RELEASE_PROT]), "home_release")


def _home_begin_drive(seed_raw):
    """Seed captured; fire the single stall-seeking 0xFD toward the closed seat."""
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
    _drain_uart_buffer()
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
    global _pending_cmd
    if func == CMD_READ_ENCODER:
        raw = _read_int48(payload)
        label = _pending_cmd
        _pending_cmd = None
        if label == "sync_read":
            _on_sync_read(raw)
        elif label == "move_sync":
            _finalize_move_to(_encoder_pos_steps(raw))
        elif label == "nudge_sync":
            _finalize_nudge_to(_encoder_pos_steps(raw))
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
            _send_and_expect(bytes([CMD_READ_ENCODER]), "nudge_sync")
        elif status == 0:
            _pending_cmd = None
            _motion_ctx = None
            _send(bytes([CMD_ENABLE, 0x00]))
            state = "idle"
            print("Valve: nudge stalled -- hit a stop, de-energized")
        return
    # normal move
    if status == 2:
        _pending_cmd = None
        _motion_ctx = None
        _send_and_expect(bytes([CMD_READ_ENCODER]), "move_sync")
    elif status == 0:
        _pending_cmd = None
        _motion_ctx = None
        state = "stalled"
        last_error = "move_stalled"
        move_in_flight_delta = 0
        _breath_enabled = False
        _send(bytes([CMD_STOP]))
        _send(bytes([CMD_RELEASE_PROT]))
        _send(bytes([CMD_ENABLE, 0x00]))
        print("Valve: move stalled (status=0) -- motor disabled")


def _cmd_started():
    global _pending_cmd, _cmd_sent_ms
    _pending_cmd = "move_done"
    _cmd_sent_ms = supervisor.ticks_ms()


def _on_prot_read(status):
    """0x3E backstop read fired by the homing timeout: 1 = latched stall = contact."""
    global _pending_cmd
    _pending_cmd = None
    if state == "homing" and status == 1:
        _home_on_contact()
    elif state == "homing":
        print("Valve: home timeout, no stall latched -- aborting")
        _send(bytes([CMD_ENABLE, 0x00]))
        _set_error("home_timeout")


def _set_error(err):
    global state, last_error
    state = "error"
    last_error = err


def _drain_uart_into_buf():
    global _rx_buf
    data = uart.read(64)
    if data:
        _rx_buf.extend(data)


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
        # the homing stall-seek runs much longer than a normal move; its primary
        # completion is the status=0 stall reply, so give it the full home budget.
        timeout = HOME_TIMEOUT_MS if _motion_ctx == "home_drive" else MOVE_TIMEOUT_MS
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
        _finalize_move_to(motor_pos_steps + move_in_flight_delta)
        return
    if label == "nudge_sync":
        _pending_cmd = None
        _finalize_nudge_to(motor_pos_steps + _nudge_delta)
        return
    if label in ("home_seed", "home_contact_read", "home_backoff_read", "home_zero_seed"):
        print(f"Valve: re-reading encoder after {label} timeout")
        _send_and_expect(bytes([CMD_READ_ENCODER]), label)
        return
    if label == "home_release":
        _pending_cmd = None
        _send_and_expect(bytes([CMD_READ_ENCODER]), "home_seed")
        return
    if label == "move_done" and _motion_ctx == "home_drive":
        # The seek may have stalled and latched silently (no status=0). Check 0x3E.
        _pending_cmd = None
        _send_and_expect(bytes([CMD_READ_SHAFT_PROT]), "home_prot_check")
        return
    # generic: cut motion + de-energize
    _send(bytes([CMD_CONSTANT_SPEED, 0x00, 0x00, 0x00]))
    _send(bytes([CMD_STOP]))
    _send(bytes([CMD_ENABLE, 0x00]))
    _breath_enabled = False
    _breath_last_rpm = None
    _motion_ctx = None
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


def _set_current(ma):
    ma = max(0, min(3000, int(ma)))
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
    _nudge_delta = steps if deg > 0 else -steps
    state = "nudging"
    _motion_ctx = "nudge"
    print(f"Valve: nudge {deg} deg ({steps} steps)")
    _pending_move = _speed_body(CMD_MOVE_POS, direction, MOVE_RPM, MOVE_ACC, steps)
    _send(bytes([CMD_ENABLE, 0x01]))
    _move_settle_at = (supervisor.ticks_ms() + MOVE_SETTLE_MS) & 0x3FFFFFFF


# ── Init ─────────────────────────────────────────────────────────────────────

def _blocking_drain():
    global _rx_buf
    while True:
        avail = uart.in_waiting
        if avail <= 0:
            break
        uart.read(avail)
    _rx_buf = bytearray()


def _blocking_wait_status(timeout_ms=500):
    """Read until a valid FB <addr> <func> <status> <crc> 5-byte frame; return status."""
    deadline = (supervisor.ticks_ms() + timeout_ms) & 0x3FFFFFFF
    buf = bytearray()
    while True:
        if _ticks_diff(supervisor.ticks_ms(), deadline) < 0x1FFFFFFF:
            return None
        data = uart.read(uart.in_waiting or 1)
        if data:
            buf.extend(data)
        while len(buf) >= 5:
            if buf[0] != RX_HEAD:
                del buf[0]
                continue
            if _checksum(buf[0:4]) != buf[4]:
                del buf[0]
                continue
            status = buf[3]
            del buf[0:5]
            return status
        time.sleep(0.005)


def _blocking_setup(body, timeout_ms=600):
    _blocking_drain()
    _send(body)
    return _blocking_wait_status(timeout_ms)


def _blocking_read_encoder(timeout_ms=600):
    """Blocking 0x31 read (motor must be stopped). Returns int48 raw, or None."""
    _blocking_drain()
    _send(bytes([CMD_READ_ENCODER]))
    deadline = (supervisor.ticks_ms() + timeout_ms) & 0x3FFFFFFF
    buf = bytearray()
    total = 3 + 6 + 1
    while True:
        if _ticks_diff(supervisor.ticks_ms(), deadline) < 0x1FFFFFFF:
            return None
        data = uart.read(uart.in_waiting or 1)
        if data:
            buf.extend(data)
        while len(buf) >= total:
            if buf[0] != RX_HEAD or buf[2] != CMD_READ_ENCODER:
                del buf[0]
                continue
            if _checksum(buf[0:total - 1]) != buf[total - 1]:
                del buf[0]
                continue
            raw = _read_int48(bytes(buf[3:9]))
            del buf[0:total]
            return raw
        time.sleep(0.005)


def init():
    """Configure the 42D for serial closed-loop control. Blocking; runs once at boot."""
    global state, last_error
    print("Valve(42D): init UART D6/D7 @ 38400")
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
              and _blocking_setup(bytes([CMD_SET_PROTECT, 0x01])) == 1
              and _blocking_setup(bytes([CMD_ENABLE, 0x01])) == 1)
        if ok:
            _send(bytes([CMD_STOP]))
            _send(bytes([CMD_ENABLE, 0x00]))   # de-energize at idle; valve self-holds
            print("Valve(42D): init OK (de-energized) -- must home before moves")
            state = "unknown"
            return
        print("Valve(42D): init attempt", attempt, "-- setup ACK failed")
        time.sleep(0.2)
    print("Valve(42D): init FAILED -- check SR_vFOC support, baud 38400, wiring")
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
    rpm = min(rpm, BREATH_MAX_RPM)
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
    motor_pos_steps = max(0, min(open_steps, motor_pos_steps))


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
    if _breath_at_valley:
        _breath_at_valley = False
        _send_and_expect(bytes([CMD_READ_ENCODER]), "breath_read")
        return
    period_ms = max(100, _breath_period_ms)
    t = _ticks_diff(now, _breath_phase_start_ms) % period_ms
    crossed_valley = t < _breath_prev_t
    _breath_prev_t = t
    if crossed_valley:
        _breath_at_valley = True
        _send(bytes([CMD_CONSTANT_SPEED, 0x00, 0x00, BREATH_ACC]))   # halt for a clean read
        _breath_last_rpm = None
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
    motor_pos_steps = max(0, min(open_steps, _encoder_pos_steps(raw)))
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
    print("Valve: streaming @ %dHz base=%dms%s"
          % (_stream_rate, _stream_base_ms, "" if homed else " (open-loop, NOT homed)"))


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
    _send(bytes([CMD_CONSTANT_SPEED, 0x00, 0x00, BREATH_ACC]))   # hold position
    _breath_last_rpm = None
    _stream_max_idx = -1
    _stream_played = -1
    if state == "streaming":
        state = "idle"
    print("Valve: stream stop")


def _stream_pong(payload):
    token = ((payload[0] << 8) | payload[1]) if len(payload) >= 2 else 0
    _stream_out.append((TOPIC_VALVE_PONG, "%d %d" % (token, supervisor.ticks_ms())))


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
            _send(bytes([CMD_CONSTANT_SPEED, 0x00, 0x00, BREATH_ACC]))
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
    if rpm > STREAM_MAX_RPM:
        rpm = STREAM_MAX_RPM
    if rpm == 0:
        if _breath_last_rpm not in (None, 0):
            _send(bytes([CMD_CONSTANT_SPEED, 0x00, 0x00, BREATH_ACC]))
            _breath_last_rpm = None
    else:
        direction = DIR_TOWARD_OPEN if delta > 0 else DIR_TOWARD_CLOSED
        _send(_speed_body(CMD_CONSTANT_SPEED, direction, rpm, 0))   # acc=0: snap
        _breath_last_rpm = rpm if delta > 0 else -rpm
    motor_pos_steps = target_step                   # open-loop: commanded IS position
    _stream_played = idx
    if _ticks_diff(now, _stream_last_telem_ms) >= STREAM_TELEM_MS:
        _stream_last_telem_ms = now
        _stream_out.append((TOPIC_VALVE_STREAMPOS, "%d %.3f" % (cur_play, _pos_fraction())))


# ── RGB status LED ────────────────────────────────────────────────────────────

def _led_init():
    global _led
    try:
        import pwmio
        _led = (pwmio.PWMOut(board.LED_RED, frequency=1000, duty_cycle=65535),
                pwmio.PWMOut(board.LED_GREEN, frequency=1000, duty_cycle=65535),
                pwmio.PWMOut(board.LED_BLUE, frequency=1000, duty_cycle=65535))
    except Exception as e:
        print("Valve: RGB LED unavailable:", e)
        _led = None


def _update_led(now):
    """Map actuation level to the onboard RGB LED: faint blue pilot when closed ->
    bright red/orange as the valve opens. Pins are active-low (duty 0 = full on)."""
    global _led_last_ms
    if _led is None or _ticks_diff(now, _led_last_ms) < LED_UPDATE_MS:
        return
    _led_last_ms = now
    f = _pos_fraction()
    f = 0.0 if f < 0.0 else 1.0 if f > 1.0 else f
    r, g, b = f, f * f * 0.5, (1.0 - f) * 0.2
    _led[0].duty_cycle = 65535 - int(r * 65535)
    _led[1].duty_cycle = 65535 - int(g * 65535)
    _led[2].duty_cycle = 65535 - int(b * 65535)


# ── Service loop ─────────────────────────────────────────────────────────────

def service():
    global pending_target, last_target_ms, state, last_error
    global target_pos_steps, _pending_move, _pending_sync_target, _pending_jump_target

    now = supervisor.ticks_ms()
    _drain_uart_into_buf()
    while len(_rx_buf) >= 5:
        if not _parse_response():
            break
    _check_timeout()
    _check_mks_silence(now)

    # Fire a staged move once its post-ENABLE settle elapsed.
    if (_pending_move is not None and _pending_cmd is None
            and _ticks_diff(now, _move_settle_at) < 0x1FFFFFFF):
        mv = _pending_move
        _pending_move = None
        _send_and_expect(mv, "move_start")

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

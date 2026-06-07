# valve.py -- MKS SERVO42D as a plain stepper via STEP/DIR/EN (pulse interface).
#
# No CAN/UART/encoder: we dead-reckon position from the step rate (the 42D closes its
# own loop internally to the pulses we send). The board glue (code.py) injects the pins
# before init():
#   valve.step = pwmio.PWMOut(STEP, frequency=1000, duty_cycle=0, variable_frequency=True)
#   valve.dir  = digitalio.DigitalInOut(DIR)   # output
#   valve.en   = digitalio.DigitalInOut(EN)    # output
#
# Motion primitive: _set_velocity(steps_per_sec) -- sets DIR + the STEP PWM frequency,
# integrating position. Breath, the bush-cue stream, and target moves all use it.

import json
import math
import supervisor

step = None     # pwmio.PWMOut on STEP (variable_frequency), duty 0 = stopped
dir = None      # digitalio.DigitalInOut on DIR
en = None       # digitalio.DigitalInOut on EN

# ── Config (VERIFY on the bench) ─────────────────────────────────────────────
MICROSTEP       = 16
STEPS_PER_REV   = 200 * MICROSTEP            # must match the 42D's on-board microstep
OPEN_STEPS      = 2000                        # PLACEHOLDER -- calibrate full travel
EN_ACTIVE_LOW   = True                        # MKS EN: enabled when LOW. VERIFY.
DIR_OPEN_LEVEL  = False                       # DIR level that opens (verified 2026-06-06; True was inverted)
MAX_SPS         = 24000                       # step-rate ceiling
MOVE_SPS        = STEPS_PER_REV * 2           # target-move speed (~2 rev/s)
TARGET_DEADBAND = max(4, STEPS_PER_REV // 100)

# ── MQTT / line-protocol topics ──────────────────────────────────────────────
TOPIC_VALVE_TARGET    = b"bush/fire/valve/target"
TOPIC_VALVE_HOME      = b"bush/fire/valve/home"
TOPIC_VALVE_STOP      = b"bush/fire/valve/stop"
TOPIC_VALVE_CALIBRATE = b"bush/fire/valve/calibrate"
TOPIC_VALVE_BREATH    = b"bush/fire/valve/breath"
TOPIC_VALVE_MAXTORQUE = b"bush/fire/valve/maxtorque"   # no-op on step/dir (current set on 42D menu)
TOPIC_VALVE_NUDGE     = b"bush/fire/valve/nudge"
TOPIC_VALVE_ACTUAL    = b"bush/fire/valve/actual"
TOPIC_VALVE_STATUS    = b"bush/fire/valve/status"
TOPIC_VALVE_ONLINE    = b"bush/fire/valve/online"
TOPIC_VALVE_PONG      = b"bush/fire/valve/pong"
TOPIC_VALVE_STREAMPOS = b"bush/fire/valve/streampos"
ALL_VALVE_TOPICS = [TOPIC_VALVE_TARGET, TOPIC_VALVE_HOME, TOPIC_VALVE_STOP,
                    TOPIC_VALVE_CALIBRATE, TOPIC_VALVE_BREATH, TOPIC_VALVE_MAXTORQUE,
                    TOPIC_VALVE_NUDGE]

# ── Streamed-waveform framing (host -> us) ───────────────────────────────────
STREAM_SENTINEL = 0xF5
SF_START, SF_SAMPLES, SF_STOP, SF_PING = 0x01, 0x02, 0x03, 0x05
STREAM_CAP      = 256
STREAM_TELEM_MS = 200

# ── State ────────────────────────────────────────────────────────────────────
state            = "idle"          # idle | breathing | streaming
homed            = True            # no homing on step/dir; boot position = 0
motor_pos_steps  = 0               # dead-reckoned
target_pos_steps = 0
open_steps       = OPEN_STEPS
last_error       = None
pending_target   = None
last_target_ms   = 0
TARGET_MIN_MS    = 100
_cur_sps         = 0               # current signed steps/sec
_last_motion_ms  = 0
_energized       = False

# breath
_breath_enabled    = True
_breath_amplitude  = 0.04
_breath_period_ms  = 5000
_breath_skew       = 0.5
_breath_phase_start_ms = 0
_breath_last_update_ms = 0
BREATH_UPDATE_MS   = 100
BREATH_DRIFT_TAU_S = 2.0

# stream
_stream_buf      = bytearray(STREAM_CAP)
_stream_max_idx  = -1
_stream_played   = -1
_stream_rate     = 30
_stream_base_ms  = 0
_stream_epoch    = 0
_stream_last_telem_ms = 0
_stream_out      = []

# telemetry / LED
_last_status_ms = 0
_last_actual_ms = 0
STATUS_MS = 500
ACTUAL_MS = 250
_led = None
_led_mode = None
_led_last_ms = 0
LED_UPDATE_MS = 60


def _ticks_diff(a, b):
    return (a - b) & 0x3FFFFFFF


def _pos_fraction():
    return motor_pos_steps / open_steps if open_steps > 0 else 0.0


def _energize(on):
    global _energized
    _energized = on
    if en is not None:
        en.value = (not on) if EN_ACTIVE_LOW else on


def _set_velocity(sps):
    """Drive at `sps` signed steps/sec (+ = toward open). Integrates position from the
    previously-held velocity, sets DIR + STEP PWM, clamps at the travel limits."""
    global _cur_sps, motor_pos_steps, _last_motion_ms
    now = supervisor.ticks_ms()
    if _last_motion_ms:
        dt = _ticks_diff(now, _last_motion_ms) / 1000.0
        motor_pos_steps += int(_cur_sps * dt)
        motor_pos_steps = max(0, min(open_steps, motor_pos_steps))
    _last_motion_ms = now
    sps = int(max(-MAX_SPS, min(MAX_SPS, sps)))
    if sps > 0 and motor_pos_steps >= open_steps:
        sps = 0
    if sps < 0 and motor_pos_steps <= 0:
        sps = 0
    _cur_sps = sps
    if step is None:
        return
    if sps == 0:
        step.duty_cycle = 0
        return
    if not _energized:
        _energize(True)
    if dir is not None:
        dir.value = DIR_OPEN_LEVEL if sps > 0 else (not DIR_OPEN_LEVEL)
    try:
        step.frequency = abs(sps)
    except (ValueError, OSError):
        pass
    step.duty_cycle = 1 << 15      # 50%


def init():
    print("Valve(42D step/dir): init -- boot position = 0, ready")
    _led_init()
    _energize(False)               # de-energized at idle; the lead screw self-holds


def cmd_stop():
    global state, pending_target, target_pos_steps
    _set_velocity(0)
    _energize(False)
    pending_target = None
    target_pos_steps = motor_pos_steps
    if state != "error":
        state = "idle"
    print("Valve: STOP")


def _zero_here():
    """`home` with no feedback: declare the current position to be 0."""
    global motor_pos_steps, target_pos_steps, state, pending_target
    _set_velocity(0)
    motor_pos_steps = 0
    target_pos_steps = 0
    pending_target = None
    if state != "error":
        state = "idle"
    print("Valve: zeroed here (no homing on step/dir)")


# ── Breath ───────────────────────────────────────────────────────────────────

def _breath_sps(now):
    period_ms = max(100, _breath_period_ms)
    s = max(0.05, min(0.95, _breath_skew))
    t = _ticks_diff(now, _breath_phase_start_ms) % period_ms
    rise = int(s * period_ms)
    if t < rise:
        phase = -math.pi / 2 + math.pi * t / rise
        dphase = math.pi / (rise / 1000.0)
    else:
        fall = period_ms - rise
        phase = math.pi / 2 + math.pi * (t - rise) / fall
        dphase = math.pi / (fall / 1000.0)
    osc = _breath_amplitude * math.cos(phase) * dphase            # frac/sec
    drift = (target_pos_steps - motor_pos_steps) / open_steps / BREATH_DRIFT_TAU_S \
        if open_steps > 0 else 0.0
    return (osc + drift) * open_steps                             # steps/sec


def _enter_breathing(now):
    global state, _breath_phase_start_ms, _breath_last_update_ms
    state = "breathing"
    _breath_phase_start_ms = now
    _breath_last_update_ms = 0
    print("Valve: breathing -- baseline=%d A=%.3f T=%dms" %
          (target_pos_steps, _breath_amplitude, _breath_period_ms))


def _service_breath(now):
    global _breath_last_update_ms
    if _ticks_diff(now, _breath_last_update_ms) < BREATH_UPDATE_MS:
        return
    _breath_last_update_ms = now
    _set_velocity(_breath_sps(now))


# ── Streamed waveform playback (open-loop; host clock is authority) ───────────

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
    global state, _stream_rate, _stream_base_ms, _stream_epoch, _stream_max_idx, _stream_played
    if len(payload) < 6:
        return
    _stream_rate = max(1, (payload[0] << 8) | payload[1])
    _stream_base_ms = int.from_bytes(payload[2:6], "big")
    _stream_epoch = supervisor.ticks_ms()
    _stream_max_idx = -1
    _stream_played = -1
    state = "streaming"
    print("Valve: streaming @ %dHz base=%dms" % (_stream_rate, _stream_base_ms))


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
    global state, _stream_max_idx, _stream_played
    _set_velocity(0)
    _stream_max_idx = -1
    _stream_played = -1
    if state == "streaming":
        state = "idle"
    print("Valve: stream stop")


def _stream_pong(payload):
    token = ((payload[0] << 8) | payload[1]) if len(payload) >= 2 else 0
    _stream_out.append((TOPIC_VALVE_PONG, "%d %d" % (token, supervisor.ticks_ms())))


def _service_stream(now):
    global _stream_played, _stream_last_telem_ms
    cur_play = _stream_base_ms + _ticks_diff(now, _stream_epoch)
    if cur_play < 0:
        return
    idx = (cur_play * _stream_rate) // 1000
    if idx <= _stream_played:
        return
    if idx > _stream_max_idx:                 # underrun -> hold
        _set_velocity(0)
        return
    oldest = _stream_max_idx - STREAM_CAP + 1
    if idx < oldest:
        idx = oldest
    target_step = _stream_buf[idx % STREAM_CAP] * open_steps // 255
    dt = (idx - _stream_played) / _stream_rate if _stream_played >= 0 else 1.0 / _stream_rate
    _set_velocity((target_step - motor_pos_steps) / dt if dt > 0 else 0)
    _stream_played = idx
    if _ticks_diff(now, _stream_last_telem_ms) >= STREAM_TELEM_MS:
        _stream_last_telem_ms = now
        _stream_out.append((TOPIC_VALVE_STREAMPOS, "%d %.3f" % (cur_play, _pos_fraction())))


# ── Commands ─────────────────────────────────────────────────────────────────

def handle_mqtt(topic, payload):
    global pending_target, open_steps
    if topic == TOPIC_VALVE_TARGET:
        v = _parse_float(payload)
        if v is not None:
            pending_target = max(0.0, min(1.0, v))
    elif topic == TOPIC_VALVE_HOME:
        _zero_here()
    elif topic == TOPIC_VALVE_STOP:
        cmd_stop()
    elif topic == TOPIC_VALVE_CALIBRATE:
        s = _parse_int(payload)
        if s is not None and 100 <= s <= 1000000:
            open_steps = s
            print("Valve: open_steps = %d" % open_steps)
    elif topic == TOPIC_VALVE_BREATH:
        _handle_breath_payload(payload)
    elif topic == TOPIC_VALVE_NUDGE:
        _cmd_nudge(_parse_int(payload))
    # TOPIC_VALVE_MAXTORQUE: ignored (42D run current is set on its menu in pulse mode)


def _cmd_nudge(deg):
    global motor_pos_steps
    if deg is None:
        return
    steps = (max(-360, min(360, deg)) * STEPS_PER_REV) // 360
    target = max(0, min(open_steps, motor_pos_steps + steps))   # +deg = toward open
    # quick blocking-ish jog via a brief velocity pulse handled by the idle servo loop
    global target_pos_steps, pending_target
    target_pos_steps = target
    pending_target = None
    print("Valve: nudge %s deg -> %d" % (deg, target))


def _handle_breath_payload(payload):
    global _breath_enabled, _breath_amplitude, _breath_period_ms, _breath_skew
    if isinstance(payload, (bytes, bytearray)):
        try:
            payload = payload.decode("utf-8")
        except UnicodeError:
            return
    try:
        d = json.loads(payload)
    except (ValueError, TypeError):
        return
    if not isinstance(d, dict):
        return
    if "amplitude" in d:
        try: _breath_amplitude = max(0.0, min(0.5, float(d["amplitude"])))
        except (ValueError, TypeError): pass
    if "period_ms" in d:
        try: _breath_period_ms = max(100, min(60000, int(d["period_ms"])))
        except (ValueError, TypeError): pass
    if "skew" in d:
        try: _breath_skew = max(0.05, min(0.95, float(d["skew"])))
        except (ValueError, TypeError): pass
    if "enabled" in d:
        _breath_enabled = bool(d["enabled"])
    print("Valve: breath A=%.3f T=%dms skew=%.2f en=%s" %
          (_breath_amplitude, _breath_period_ms, _breath_skew, _breath_enabled))


def _parse_float(payload):
    if isinstance(payload, (bytes, bytearray)):
        try: payload = payload.decode("utf-8")
        except UnicodeError: return None
    try: return float(payload)
    except (ValueError, TypeError): return None


def _parse_int(payload):
    if isinstance(payload, (bytes, bytearray)):
        try: payload = payload.decode("utf-8")
        except UnicodeError: return None
    try: return int(payload)
    except (ValueError, TypeError): return None


# ── Service loop ─────────────────────────────────────────────────────────────

def service():
    global pending_target, last_target_ms, target_pos_steps, state
    now = supervisor.ticks_ms()

    # apply a new target / breath baseline at most every TARGET_MIN_MS
    if pending_target is not None and _ticks_diff(now, last_target_ms) >= TARGET_MIN_MS:
        last_target_ms = now
        target_pos_steps = int(round(pending_target * open_steps))
        pending_target = None
        if _breath_enabled and state in ("idle", "breathing"):
            if state != "breathing":
                _enter_breathing(now)

    if state == "breathing":
        _service_breath(now)
    elif state == "streaming":
        _service_stream(now)
    else:  # idle: servo toward target_pos_steps (deadband), else de-energize
        err = target_pos_steps - motor_pos_steps
        if err > TARGET_DEADBAND:
            _set_velocity(MOVE_SPS)
        elif err < -TARGET_DEADBAND:
            _set_velocity(-MOVE_SPS)
        else:
            if _cur_sps != 0:
                _set_velocity(0)
            elif _energized and target_pos_steps == motor_pos_steps:
                _energize(False)

    _update_led(now)


# ── Telemetry ─────────────────────────────────────────────────────────────────

def _status_json():
    return json.dumps({"state": state, "pos": round(_pos_fraction(), 3),
                       "target": round(target_pos_steps / open_steps, 3) if open_steps else 0,
                       "homed": homed, "stalled": False, "last_error": last_error})


def get_publish_messages():
    global _last_status_ms, _last_actual_ms
    now = supervisor.ticks_ms()
    msgs = []
    if _stream_out:
        msgs.extend(_stream_out)
        _stream_out[:] = []
    if _ticks_diff(now, _last_status_ms) >= STATUS_MS:
        _last_status_ms = now
        msgs.append((TOPIC_VALVE_STATUS, _status_json()))
    if _ticks_diff(now, _last_actual_ms) >= ACTUAL_MS:
        _last_actual_ms = now
        msgs.append((TOPIC_VALVE_ACTUAL, str(round(_pos_fraction(), 3))))
    return msgs


# ── RGB / status LED ──────────────────────────────────────────────────────────

def _led_init():
    global _led, _led_mode
    import board
    try:
        import pwmio
        _led = (pwmio.PWMOut(board.LED_RED, frequency=1000, duty_cycle=65535),
                pwmio.PWMOut(board.LED_GREEN, frequency=1000, duty_cycle=65535),
                pwmio.PWMOut(board.LED_BLUE, frequency=1000, duty_cycle=65535))
        _led_mode = "rgb"
    except Exception:
        try:
            import digitalio
            d = digitalio.DigitalInOut(board.LED)
            d.direction = digitalio.Direction.OUTPUT
            _led = (d,)
            _led_mode = "digital"
        except Exception as e:
            print("Valve: status LED unavailable:", e)
            _led = None


def _update_led(now):
    global _led_last_ms
    if _led is None or _ticks_diff(now, _led_last_ms) < LED_UPDATE_MS:
        return
    _led_last_ms = now
    f = _pos_fraction()
    f = 0.0 if f < 0.0 else 1.0 if f > 1.0 else f
    if _led_mode == "rgb":
        _led[0].duty_cycle = 65535 - int(f * 65535)
        _led[1].duty_cycle = 65535 - int(f * f * 0.5 * 65535)
        _led[2].duty_cycle = 65535 - int((1.0 - f) * 0.2 * 65535)
    else:
        _led[0].value = f > 0.10

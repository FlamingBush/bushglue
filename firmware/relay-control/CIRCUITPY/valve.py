# valve.py — Motorized needle valve control via MKS SERVO42C-MT V1.1
# UART binary protocol over GP4 (TX) / GP5 (RX)
#
# Position model: 0 steps = closed, open_steps = fully open.
# Position is tracked from commanded pulse counts only — the encoder
# (`raw_angle` in status) is exposed for diagnostics but never used
# to compute current_pos, so there's no encoder-polarity assumption.
# The MKS firmware is closed-loop in CR_UART mode and holds position
# at the configured current after a move's pulses have been issued.
#
# Stall protection (the MKS's on-screen "Protect" feature) handles the
# endstops: when the motor blocks, the MKS halts itself and reports
# 0x01 on read_stall (0x3E). We poll for that during both homing and
# normal moves and re-enable the driver afterward to clear the trigger.

import board
import busio
import supervisor
import json

uart = busio.UART(board.GP4, board.GP5, baudrate=115200, timeout=0.1)

DEBUG = False

MKS_ADDR          = 0xE0
CMD_READ_ANGLE    = 0x36
CMD_READ_STALL    = 0x3E
CMD_ENABLE        = 0xF3
CMD_STOP          = 0xF7
CMD_MOVE_POS      = 0xFD
CMD_SET_CURRENT   = 0x83
CMD_SET_MICROSTEP = 0x84
CMD_SET_MODE      = 0x82

OPEN_STEPS    = 16000
MAX_SPEED     = 20
HOME_SPEED    = 10
CURRENT_GEAR  = 0x01
MICROSTEP     = 16

TOPIC_VALVE_TARGET    = b"bush/fire/valve/target"
TOPIC_VALVE_HOME      = b"bush/fire/valve/home"
TOPIC_VALVE_STOP      = b"bush/fire/valve/stop"
TOPIC_VALVE_CALIBRATE = b"bush/fire/valve/calibrate"
TOPIC_VALVE_ACTUAL    = b"bush/fire/valve/actual"
TOPIC_VALVE_STATUS    = b"bush/fire/valve/status"
TOPIC_VALVE_ONLINE    = b"bush/fire/valve/online"

ALL_VALVE_TOPICS = [
    TOPIC_VALVE_TARGET,
    TOPIC_VALVE_HOME,
    TOPIC_VALVE_STOP,
    TOPIC_VALVE_CALIBRATE,
]

# state ∈ {"unknown", "initializing", "homing", "idle", "stalled", "error"}
state           = "unknown"
current_pos     = 0
target_pos      = 0
open_steps      = OPEN_STEPS
homed           = False
last_error      = None
last_raw_angle  = 0

last_status_ms  = 0
last_actual_ms  = 0
STATUS_IDLE_MS  = 1000
STATUS_MOVE_MS  = 200
ACTUAL_IDLE_MS  = 1000
ACTUAL_MOVE_MS  = 200

pending_target  = None
last_target_ms  = 0
TARGET_MIN_MS   = 100
# Inbound MQTT-target sample rate: ignore target messages arriving faster
# than this so multiple publishers can't combine to overrun the main loop.
# Only the most recent value within a window matters anyway.
_last_target_inbound_ms = 0
TARGET_INBOUND_MIN_MS = 200

_rx_buf         = bytearray()
_pending_cmd    = None
_cmd_sent_ms    = 0
CMD_TIMEOUT_MS  = 500

# Init state machine. Each entry is the command bytes; we _send_and_expect
# them one at a time, advancing only after each ack arrives, so the acks
# can't pile up and get misattributed.
_INIT_STEPS = [
    bytes([CMD_STOP]),
    bytes([CMD_SET_MODE, 0x02]),
    bytes([CMD_SET_MICROSTEP, MICROSTEP]),
    bytes([CMD_SET_CURRENT, CURRENT_GEAR]),
    bytes([CMD_ENABLE, 0x01]),
]
_init_idx = 0

# Homing sub-phases: None | "settle" | "drive" | "running"
_home_started_ms      = 0
HOME_TIMEOUT_MS       = 30000
_last_stall_poll_ms   = 0
HOME_POLL_MS          = 200
_home_phase           = None
HOME_SETTLE_MS        = 50
_home_settle_until_ms = 0


def _ticks_diff(later, earlier):
    return (later - earlier) & 0x3FFFFFFF


def _checksum(data):
    return sum(data) & 0xFF


def _hex(buf):
    return " ".join("%02X" % b for b in buf)


def _send(cmd_bytes):
    pkt = bytes([MKS_ADDR]) + cmd_bytes
    pkt = pkt + bytes([_checksum(pkt)])
    if DEBUG:
        print("VALVE TX:", _hex(pkt))
    uart.write(pkt)


def _send_and_expect(cmd_bytes, label):
    global _pending_cmd, _cmd_sent_ms
    if DEBUG and _pending_cmd is not None:
        print("VALVE WARN: overwriting pending %r with %r" % (_pending_cmd, label))
    _pending_cmd = label
    _cmd_sent_ms = supervisor.ticks_ms()
    _send(cmd_bytes)


def cmd_stop():
    global _pending_cmd, state, target_pos
    _send(bytes([CMD_STOP]))
    _pending_cmd = None
    target_pos = current_pos
    if state == "homing":
        state = "idle"
    print("Valve STOP")


def cmd_move(step_target):
    global target_pos, last_error, current_pos
    step_target = max(0, min(open_steps, step_target))
    target_pos = step_target

    if not homed:
        last_error = "not_homed"
        print("Valve: rejecting MOVE — not homed")
        return

    delta = step_target - current_pos
    if delta == 0:
        return

    direction = 0x80 if delta > 0 else 0x00
    abs_pulses = abs(delta)
    speed_dir = direction | (MAX_SPEED & 0x7F)
    _send_and_expect(
        bytes([CMD_MOVE_POS, speed_dir]) + abs_pulses.to_bytes(4, "big"),
        "move",
    )
    current_pos = step_target  # MKS closed-loop owns the actual motion


def _read_angle():
    _send_and_expect(bytes([CMD_READ_ANGLE]), "read_angle")


def _read_stall():
    _send_and_expect(bytes([CMD_READ_STALL]), "read_stall")


def cmd_home():
    global state, _home_started_ms, homed, last_error
    global _last_stall_poll_ms, _home_phase, _home_settle_until_ms
    homed = False
    last_error = None
    state = "homing"
    now = supervisor.ticks_ms()
    _home_started_ms = now
    _last_stall_poll_ms = now
    _home_phase = "settle"
    _home_settle_until_ms = (now + HOME_SETTLE_MS) & 0x3FFFFFFF
    print("Valve: homing — driving toward open stop, watching for stall...")


def _on_stall_detected():
    """Common handling for read_stall returning 0x01."""
    global state, homed, current_pos, target_pos, last_error
    global _home_phase
    if state == "homing" and _home_phase == "running":
        homed = True
        current_pos = open_steps
        target_pos = current_pos
        state = "idle"
        last_error = None
        _home_phase = None
        _send_and_expect(bytes([CMD_ENABLE, 0x01]), "enable")  # clear Wrong Protect
        print("Valve: homed at open stop")
    else:
        homed = False
        state = "stalled"
        last_error = "stalled"
        _home_phase = None
        _send_and_expect(bytes([CMD_ENABLE, 0x01]), "enable")  # clear Wrong Protect
        print("Valve: STALLED during move — re-home required")


def _parse_response():
    global _rx_buf, _pending_cmd, state, last_error, last_raw_angle
    global _init_idx

    if len(_rx_buf) < 3:
        return False
    if _rx_buf[0] != MKS_ADDR:
        _rx_buf = _rx_buf[1:]
        return True

    cmd = _pending_cmd

    if cmd == "read_angle":
        if len(_rx_buf) < 6:
            return False
        if _checksum(_rx_buf[0:5]) != _rx_buf[5]:
            # bad checksum (likely an echo prefix or stray byte) — resync
            if DEBUG:
                print("VALVE: resync read_angle, dropping", _hex(_rx_buf[0:1]))
            _rx_buf = _rx_buf[1:]
            return True
        raw_u32 = int.from_bytes(_rx_buf[1:5], "big")
        last_raw_angle = raw_u32 - 0x100000000 if raw_u32 & 0x80000000 else raw_u32
        _rx_buf = _rx_buf[6:]
        _pending_cmd = None
        return True

    if cmd == "read_stall":
        if _rx_buf[1] not in (0x00, 0x01, 0x02):
            if DEBUG:
                print("VALVE: resync read_stall, dropping", _hex(_rx_buf[0:3]))
            _rx_buf = _rx_buf[1:]
            return True
        stall_byte = _rx_buf[1]
        _rx_buf = _rx_buf[3:]
        _pending_cmd = None
        if stall_byte == 0x01:
            _on_stall_detected()
        elif stall_byte == 0x00:
            last_error = "motor_driver_error"
            if DEBUG:
                print("Valve: motor driver reports error (0x00)")
        elif DEBUG:
            print("Valve: stall poll = running normally (0x02)")
        return True

    # XXX LAST-DITCH HACK — REVISIT.
    # On bring-up we observed RX bytes like `E0 84 10 FC` arriving before
    # the real `E0 01 E1` ack for SET_MICROSTEP / ENABLE. Source unconfirmed:
    # could be TX→RX crosstalk on GP4/GP5, an MKS firmware quirk, or our
    # own state-machine timing letting two responses overlap. Until we know,
    # we silently resync by dropping any "E0 <not 00/01>" prefix. If you're
    # debugging missing acks or weird state, START HERE — this masks signal.
    if _rx_buf[1] not in (0x00, 0x01):
        if DEBUG:
            print("VALVE: resync, dropping", _hex(_rx_buf[0:3]))
        _rx_buf = _rx_buf[1:]
        return True

    if cmd is None:
        if DEBUG:
            print("VALVE WARN: stray ack", _hex(_rx_buf[0:3]))
        _rx_buf = _rx_buf[3:]
        return True

    ok = _rx_buf[1] == 0x01
    _rx_buf = _rx_buf[3:]

    if cmd == "init":
        _pending_cmd = None
        if not ok and DEBUG:
            print("Valve: init step %d not ok'd by MKS" % _init_idx)
        _init_idx += 1
        if _init_idx < len(_INIT_STEPS):
            _send_and_expect(_INIT_STEPS[_init_idx], "init")
        else:
            state = "unknown"
            print("Valve: init complete, must home before moves")
        return True

    if cmd == "move":
        if not ok:
            state = "error"
            last_error = "move_failed"
            print("Valve: move command rejected by MKS")
        _pending_cmd = None
        return True

    if cmd == "enable":
        if not ok:
            state = "error"
            last_error = "enable_failed"
            print("Valve: enable failed")
        _pending_cmd = None
        return True

    _pending_cmd = None
    return True


def _drain_uart():
    global _rx_buf
    data = uart.read(64)
    if data:
        if DEBUG:
            print("VALVE RX:", _hex(data))
        _rx_buf.extend(data)


def _check_timeout():
    global _pending_cmd, last_error, _init_idx, state
    if _pending_cmd is None:
        return
    if _ticks_diff(supervisor.ticks_ms(), _cmd_sent_ms) < CMD_TIMEOUT_MS:
        return
    if DEBUG:
        print("Valve: UART timeout (%s)" % _pending_cmd)
    last_error = "uart_timeout_%s" % _pending_cmd
    cmd = _pending_cmd
    _pending_cmd = None
    # Keep the init state machine advancing on timeout — otherwise we'd
    # sit in "initializing" forever if any one ack never lands.
    if cmd == "init":
        _init_idx += 1
        if _init_idx < len(_INIT_STEPS):
            _send_and_expect(_INIT_STEPS[_init_idx], "init")
        else:
            state = "unknown"
            print("Valve: init complete (with timeouts)")


def handle_mqtt(topic, payload):
    global pending_target, open_steps, _last_target_inbound_ms

    if topic == TOPIC_VALVE_TARGET:
        now = supervisor.ticks_ms()
        if _ticks_diff(now, _last_target_inbound_ms) < TARGET_INBOUND_MIN_MS:
            return
        _last_target_inbound_ms = now
        try:
            val = float(payload)
        except (ValueError, TypeError):
            try:
                data = json.loads(payload)
                val = float(data.get("target", data.get("value", 0)))
            except (ValueError, TypeError, KeyError):
                print("Valve: bad target payload: %r" % payload)
                return
        pending_target = max(0.0, min(1.0, val))

    elif topic == TOPIC_VALVE_HOME:
        cmd_home()

    elif topic == TOPIC_VALVE_STOP:
        cmd_stop()

    elif topic == TOPIC_VALVE_CALIBRATE:
        text = payload.decode().strip() if isinstance(payload, (bytes, bytearray)) else str(payload).strip()
        new_steps = None
        try:
            new_steps = int(text)
        except (ValueError, TypeError):
            try:
                data = json.loads(text)
                new_steps = int(data.get("steps", data.get("value")))
            except (ValueError, TypeError, KeyError):
                pass
        if new_steps is None:
            print("Valve: bad calibrate payload: %r" % payload)
            return
        if 100 <= new_steps <= 100000:
            open_steps = new_steps
            print("Valve: open_steps calibrated to %d" % open_steps)
        else:
            print("Valve: calibrate value out of range: %d" % new_steps)


def init():
    """Begin non-blocking MKS init. Advanced by the state machine in service()."""
    global state, _init_idx, _rx_buf
    print("Valve: initializing MKS SERVO42C on GP4/GP5 at 115200 baud")
    try:
        uart.reset_input_buffer()
    except AttributeError:
        pass
    _rx_buf = bytearray()
    state = "initializing"
    _init_idx = 0
    _send_and_expect(_INIT_STEPS[0], "init")


def service():
    global pending_target, last_target_ms, state, last_error
    global _home_phase, _last_stall_poll_ms, _rx_buf

    now = supervisor.ticks_ms()

    _drain_uart()
    while len(_rx_buf) >= 3:
        if not _parse_response():
            break
    _check_timeout()

    if state == "homing":
        if _home_phase == "settle":
            if _ticks_diff(now, _home_settle_until_ms) >= 0:
                try:
                    uart.reset_input_buffer()
                except AttributeError:
                    pass
                _rx_buf = bytearray()
                pulses = (open_steps * 2).to_bytes(4, "big")
                speed_dir = 0x80 | (HOME_SPEED & 0x7F)
                _send_and_expect(bytes([CMD_MOVE_POS, speed_dir]) + pulses, "move")
                _home_phase = "running"
                _last_stall_poll_ms = now

        elif (_home_phase == "running"
              and _pending_cmd is None
              and _ticks_diff(now, _last_stall_poll_ms) >= HOME_POLL_MS):
            _last_stall_poll_ms = now
            _read_stall()

        if _ticks_diff(now, _home_started_ms) >= HOME_TIMEOUT_MS:
            state = "error"
            last_error = "home_timeout"
            _home_phase = None
            cmd_stop()
            print("Valve: homing timed out")

    if (pending_target is not None
            and _pending_cmd is None
            and state == "idle"
            and _ticks_diff(now, last_target_ms) >= TARGET_MIN_MS):
        step_target = int(pending_target * open_steps)
        pending_target = None
        last_target_ms = now
        if step_target != current_pos:
            cmd_move(step_target)


def _status_json():
    return json.dumps({
        "state": state,
        "pos": round(current_pos / open_steps, 3) if open_steps > 0 else 0,
        "target": round(target_pos / open_steps, 3) if open_steps > 0 else 0,
        "homed": homed,
        "stalled": state == "stalled",
        "last_error": last_error,
        "pos_steps": current_pos,
        "open_steps": open_steps,
        "raw_angle": last_raw_angle,
    })


def get_publish_messages():
    global last_status_ms, last_actual_ms
    now = supervisor.ticks_ms()
    msgs = []

    interval = STATUS_MOVE_MS if state == "homing" else STATUS_IDLE_MS
    if _ticks_diff(now, last_status_ms) >= interval:
        last_status_ms = now
        msgs.append((TOPIC_VALVE_STATUS, _status_json()))

    actual_interval = ACTUAL_MOVE_MS if state == "homing" else ACTUAL_IDLE_MS
    if _ticks_diff(now, last_actual_ms) >= actual_interval:
        last_actual_ms = now
        actual = current_pos / open_steps if open_steps > 0 else 0
        msgs.append((TOPIC_VALVE_ACTUAL, str(round(actual, 3))))

    return msgs

# valve.py — Motorized needle valve control via MKS SERVO42C-MT V1.1
# UART binary protocol over GP4 (TX) / GP5 (RX)
#
# Position convention:
#   0 steps  = fully closed  (CW limit when looking at motor back)
#   open_steps = fully open  (CCW limit, homing stop)
#
# Homing always drives toward open (CCW) until mechanical stall,
# then zeros there.  Soft limits prevent driving into the needle seat.
#
# MQTT targets use 0.0 = closed, 1.0 = fully open.
# step_position = target * open_steps

import board
import busio
import supervisor
import json

# ── UART setup ──────────────────────────────────────────────────────────────
uart = busio.UART(board.GP4, board.GP5, baudrate=115200, timeout=0.1)

# ── MKS SERVO42C protocol constants ────────────────────────────────────────
MKS_ADDR        = 0xE0
CMD_READ_ANGLE  = 0x36
CMD_READ_STALL  = 0x3E
CMD_ENABLE      = 0xF3
CMD_STOP        = 0xF7
CMD_MOVE_POS    = 0xFD
CMD_RETURN_ZERO = 0x94
CMD_SET_ZERO    = 0x91
CMD_SET_CURRENT = 0x83
CMD_SET_MICROSTEP = 0x84
CMD_SET_MODE    = 0x82
CMD_SET_ZERO_DIR = 0x93
CMD_SET_ZERO_MODE = 0x90
CMD_SET_ZERO_SPEED = 0x92

# ── Valve configuration ────────────────────────────────────────────────────
OPEN_STEPS      = 16000   # default; ~5 turns at 3200 steps/rev (16x microstep)
MAX_SPEED       = 20      # speed gear for normal moves (conservative)
HOME_SPEED      = 10      # speed gear for homing (slow for stall detection)
CURRENT_GEAR    = 0x01    # 200mA — absolute minimum, stall-as-fuse
MICROSTEP       = 16      # 16x microstepping

# ── MQTT topics ─────────────────────────────────────────────────────────────
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

# ── State ───────────────────────────────────────────────────────────────────
# States: "unknown", "homing", "idle", "moving", "stalled", "error"
state           = "unknown"
current_pos     = 0        # steps from closed (0) to open (open_steps)
target_pos      = 0        # desired step position
open_steps      = OPEN_STEPS
homed           = False
last_error      = None

# Rate limiting
last_status_ms  = 0
last_actual_ms  = 0
STATUS_IDLE_MS  = 1000     # publish status every 1s when idle
STATUS_MOVE_MS  = 200      # publish status every 200ms when moving (5 Hz)
ACTUAL_IDLE_MS  = 1000
ACTUAL_MOVE_MS  = 200

# Command coalescing — only the latest target matters
pending_target  = None     # float 0.0-1.0, set by MQTT, consumed by service loop
last_target_ms  = 0
TARGET_MIN_MS   = 100      # minimum 100ms between move commands (10 Hz)

# UART receive buffer
_rx_buf         = bytearray()
_pending_cmd    = None     # what we're waiting for a response to
_cmd_sent_ms    = 0
CMD_TIMEOUT_MS  = 500      # UART response timeout
_retry_count    = 0
MAX_RETRIES     = 1

# Homing state
_home_started_ms = 0
HOME_TIMEOUT_MS  = 30000   # 30s max for homing


def _ticks_diff(later, earlier):
    return (later - earlier) & 0x3FFFFFFF


def _ticks_expired(deadline):
    if deadline is None:
        return False
    return _ticks_diff(supervisor.ticks_ms(), deadline) < 0x1FFFFFFF


# ── MKS packet construction ────────────────────────────────────────────────

def _checksum(data):
    return sum(data) & 0xFF


def _send(cmd_bytes):
    """Send a raw command packet with address prefix and checksum."""
    pkt = bytes([MKS_ADDR]) + cmd_bytes
    pkt = pkt + bytes([_checksum(pkt)])
    uart.write(pkt)


def _send_and_expect(cmd_bytes, label):
    """Send command and register that we're waiting for a response."""
    global _pending_cmd, _cmd_sent_ms, _retry_count
    _pending_cmd = label
    _cmd_sent_ms = supervisor.ticks_ms()
    _retry_count = 0
    _send(cmd_bytes)


def cmd_enable(enable=True):
    _send_and_expect(bytes([CMD_ENABLE, 0x01 if enable else 0x00]), "enable")


def cmd_disable():
    _send_and_expect(bytes([CMD_ENABLE, 0x00]), "disable")


def cmd_stop():
    """Emergency stop — send immediately, don't wait for pending."""
    global _pending_cmd, state, target_pos
    _send(bytes([CMD_STOP]))
    _pending_cmd = None
    target_pos = current_pos
    if state == "moving":
        state = "idle"
    print("Valve STOP")


def cmd_move(step_target):
    """Move to absolute step position. Enforces soft limits."""
    global target_pos, _pending_cmd
    step_target = max(0, min(open_steps, step_target))
    target_pos = step_target

    if not homed:
        print("Valve: rejecting MOVE — not homed")
        return

    # Calculate relative move from current position
    delta = step_target - current_pos
    if delta == 0:
        return

    # Direction: CW (toward closed, decreasing open) = 0, CCW (toward open) = 1
    # In our convention: higher steps = more open
    # MKS: direction bit 7: 0 = CW, 1 = CCW
    # Closing (delta < 0) = CW = dir 0
    # Opening (delta > 0) = CCW = dir 1
    if delta > 0:
        direction = 0x80  # CCW — toward open
    else:
        direction = 0x00  # CW — toward closed

    abs_pulses = abs(delta)
    speed_dir = direction | (MAX_SPEED & 0x7F)
    pulse_bytes = abs_pulses.to_bytes(4, "big")

    _send_and_expect(
        bytes([CMD_MOVE_POS, speed_dir]) + pulse_bytes,
        "move"
    )


def cmd_read_angle():
    _send_and_expect(bytes([CMD_READ_ANGLE]), "read_angle")


def cmd_read_stall():
    _send_and_expect(bytes([CMD_READ_STALL]), "read_stall")


def cmd_home():
    """Initiate homing sequence: drive CCW toward open stop."""
    global state, _home_started_ms, homed
    homed = False
    state = "homing"
    _home_started_ms = supervisor.ticks_ms()
    # Use MKS built-in return-to-zero in CCW direction (toward open)
    # First set zero mode to DirMode (single direction search)
    _send(bytes([CMD_SET_ZERO_MODE, 0x01]))
    # Set zero return direction: CCW = 0x01
    _send(bytes([CMD_SET_ZERO_DIR, 0x01]))
    # Set zero return speed: slowest = 0x04
    _send(bytes([CMD_SET_ZERO_SPEED, 0x04]))
    # Execute return to zero
    _send_and_expect(bytes([CMD_RETURN_ZERO, 0x00]), "home")
    print("Valve: homing (driving CCW toward open stop)...")


def cmd_set_zero():
    """Set current position as zero (called after homing reaches open stop)."""
    _send_and_expect(bytes([CMD_SET_ZERO, 0x00]), "set_zero")


# ── UART response parsing ──────────────────────────────────────────────────

def _parse_response():
    """Try to parse a complete response from _rx_buf. Returns True if handled."""
    global _rx_buf, _pending_cmd, current_pos, state, homed, last_error
    global _home_started_ms

    if len(_rx_buf) < 3:
        return False

    # All responses start with MKS_ADDR
    if _rx_buf[0] != MKS_ADDR:
        # Discard garbage byte
        _rx_buf = _rx_buf[1:]
        return True  # try again

    cmd = _pending_cmd

    if cmd == "read_angle":
        # Response: ADDR + 4 bytes (int32) + CHK = 6 bytes total
        if len(_rx_buf) < 6:
            return False
        raw = int.from_bytes(_rx_buf[1:5], "big", signed=True)
        chk = _rx_buf[5]
        if chk == _checksum(_rx_buf[0:5]):
            # Convert encoder angle to step position
            # MKS reports 0-65535 per rotation, multi-turn accumulating
            # After homing, 0 = open position
            # We need steps from closed: current_pos = open_steps - encoder_steps
            # But encoder_steps after homing is relative to open stop
            # So: position relative to open stop in steps
            # encoder ticks per rev = 65536, steps per rev = 3200 (16x)
            # steps_from_open = raw * 3200 / 65536
            steps_from_open = (raw * 3200) // 65536
            # our convention: 0 = closed, open_steps = open
            current_pos = max(0, min(open_steps, open_steps - steps_from_open))
        _rx_buf = _rx_buf[6:]
        _pending_cmd = None
        return True

    elif cmd == "read_stall":
        # Response: ADDR + 1 byte status + CHK = 3 bytes
        if len(_rx_buf) < 3:
            return False
        stall_byte = _rx_buf[1]
        _rx_buf = _rx_buf[3:]
        _pending_cmd = None
        if stall_byte == 0x01:  # blocked/stalled
            state = "stalled"
            last_error = "motor_stalled"
            print("Valve: STALLED")
        return True

    else:
        # Generic success/fail response: ADDR + 01/00 + CHK = 3 bytes
        if len(_rx_buf) < 3:
            return False
        ok = _rx_buf[1] == 0x01
        _rx_buf = _rx_buf[3:]

        if cmd == "home":
            if ok:
                # Home complete — set this as zero and mark as open position
                homed = True
                current_pos = open_steps  # we're at the open stop
                target_pos = current_pos
                state = "idle"
                # Set current position as zero in the MKS
                cmd_set_zero()
                print(f"Valve: homed at open stop, pos={current_pos}")
            else:
                state = "error"
                last_error = "home_failed"
                print("Valve: homing FAILED")

        elif cmd == "set_zero":
            if ok:
                print("Valve: zero set at open stop")
            else:
                print("Valve: set_zero failed (non-critical)")

        elif cmd == "move":
            if ok:
                state = "moving"
            else:
                state = "error"
                last_error = "move_failed"
                print("Valve: move command rejected by MKS")

        elif cmd == "enable":
            if ok:
                print("Valve: motor enabled")
            else:
                print("Valve: enable failed")
                state = "error"
                last_error = "enable_failed"

        _pending_cmd = None
        return True


def _drain_uart():
    """Read available bytes from UART into rx buffer."""
    global _rx_buf
    data = uart.read(64)
    if data:
        _rx_buf.extend(data)


def _check_timeout():
    """Handle UART response timeout."""
    global _pending_cmd, _retry_count, state, last_error
    if _pending_cmd is None:
        return
    if _ticks_diff(supervisor.ticks_ms(), _cmd_sent_ms) < CMD_TIMEOUT_MS:
        return

    if _retry_count < MAX_RETRIES:
        _retry_count += 1
        print(f"Valve: UART timeout ({_pending_cmd}), retry {_retry_count}")
        _cmd_sent_ms = supervisor.ticks_ms()
        # Re-send is not straightforward since we don't cache the packet.
        # Just clear and let the next service cycle re-issue if needed.
        _pending_cmd = None
    else:
        print(f"Valve: UART timeout ({_pending_cmd}), giving up")
        last_error = f"uart_timeout_{_pending_cmd}"
        if state not in ("stalled", "error"):
            state = "error"
        _pending_cmd = None


# ── MQTT message handler ───────────────────────────────────────────────────

def handle_mqtt(topic, payload):
    """Called from code.py when a valve-related MQTT message arrives."""
    global pending_target, last_target_ms, open_steps

    if topic == TOPIC_VALVE_TARGET:
        try:
            val = float(payload)
        except (ValueError, TypeError):
            try:
                data = json.loads(payload)
                val = float(data.get("target", data.get("value", 0)))
            except (ValueError, TypeError, KeyError):
                print(f"Valve: bad target payload: {payload}")
                return
        # Clamp to [0, 1]
        val = max(0.0, min(1.0, val))
        pending_target = val

    elif topic == TOPIC_VALVE_HOME:
        cmd_home()

    elif topic == TOPIC_VALVE_STOP:
        cmd_stop()

    elif topic == TOPIC_VALVE_CALIBRATE:
        try:
            new_steps = int(payload)
            if isinstance(payload, (bytes, bytearray)):
                data = json.loads(payload)
                new_steps = int(data.get("steps", data.get("value", payload)))
        except (ValueError, TypeError):
            try:
                new_steps = int(payload)
            except (ValueError, TypeError):
                print(f"Valve: bad calibrate payload: {payload}")
                return
        if 100 <= new_steps <= 100000:
            open_steps = new_steps
            print(f"Valve: open_steps calibrated to {open_steps}")
        else:
            print(f"Valve: calibrate value out of range: {new_steps}")


# ── Initialization (called once from code.py after MQTT connects) ──────────

def init():
    """Initialize the MKS SERVO42C. Call once after boot."""
    global state
    print("Valve: initializing MKS SERVO42C on GP4/GP5 at 115200 baud")
    # Drain any stale data
    uart.read(256)

    # Set work mode to CR_UART
    _send(bytes([CMD_SET_MODE, 0x02]))
    # Set microstepping to 16
    _send(bytes([CMD_SET_MICROSTEP, MICROSTEP]))
    # Set current to 200mA (gear 1)
    _send(bytes([CMD_SET_CURRENT, CURRENT_GEAR]))
    # Enable motor
    cmd_enable()

    state = "unknown"
    print("Valve: init commands sent, must home before accepting moves")


# ── Service loop (called every iteration from code.py main loop) ───────────

def service():
    """Non-blocking service loop. Call as often as possible from main loop."""
    global pending_target, last_target_ms, state, last_error
    global last_status_ms, last_actual_ms, _home_started_ms

    now = supervisor.ticks_ms()

    # Drain UART
    _drain_uart()

    # Parse any complete response
    while len(_rx_buf) >= 3:
        if not _parse_response():
            break

    # Check for UART timeout
    _check_timeout()

    # Homing timeout check
    if state == "homing":
        if _ticks_diff(now, _home_started_ms) >= HOME_TIMEOUT_MS:
            state = "error"
            last_error = "home_timeout"
            cmd_stop()
            print("Valve: homing timed out")

    # Process pending target (rate limited)
    if (pending_target is not None
            and _pending_cmd is None
            and state in ("idle", "moving")
            and _ticks_diff(now, last_target_ms) >= TARGET_MIN_MS):
        step_target = int(pending_target * open_steps)
        pending_target = None
        last_target_ms = now
        if step_target != current_pos:
            cmd_move(step_target)

    # If moving and no pending command, poll position
    if state == "moving" and _pending_cmd is None:
        cmd_read_angle()

    # Check if we've reached target (within tolerance)
    if state == "moving" and abs(current_pos - target_pos) <= 5:
        state = "idle"


def _status_json():
    """Build status JSON for MQTT publication."""
    return json.dumps({
        "state": state,
        "pos": round(current_pos / open_steps, 3) if open_steps > 0 else 0,
        "target": round(target_pos / open_steps, 3) if open_steps > 0 else 0,
        "homed": homed,
        "stalled": state == "stalled",
        "last_error": last_error,
    })


def get_publish_messages():
    """Return list of (topic, payload) to publish. Called from code.py."""
    global last_status_ms, last_actual_ms
    now = supervisor.ticks_ms()
    msgs = []

    interval = STATUS_MOVE_MS if state == "moving" else STATUS_IDLE_MS
    if _ticks_diff(now, last_status_ms) >= interval:
        last_status_ms = now
        msgs.append((TOPIC_VALVE_STATUS, _status_json()))

    actual_interval = ACTUAL_MOVE_MS if state == "moving" else ACTUAL_IDLE_MS
    if _ticks_diff(now, last_actual_ms) >= actual_interval:
        last_actual_ms = now
        actual = current_pos / open_steps if open_steps > 0 else 0
        msgs.append((TOPIC_VALVE_ACTUAL, str(round(actual, 3))))

    return msgs

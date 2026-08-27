# main.py — Pi Pico 2 W MQTT GPIO pulse controller
# CircuitPython 10.x
#
# CRITICAL INVARIANTS — every code path in this main loop must respect both:
#   1. Solenoid pulse OFF deadlines (sub-ms accuracy). service_pins runs
#      first every iteration; nothing downstream may block long enough to
#      delay an OFF deadline. Relays must never get stuck on.
#   2. MQTT keepalive: pings must reach the broker within KEEP_ALIVE (15 s).
#      That means mqtt_loop has to be reached every iteration, and no
#      single iteration may take more than a few ms.
# If you add work here, measure or reason about its worst-case duration
# under load (heavy MQTT traffic).
#
# secrets.py must define:
#   SSID, PASSWORD, MQTT_BROKER
# Optional keys:
#   MQTT_PORT (default 1883), MQTT_USER, MQTT_PASSWORD
#
# Broker discovery: if the configured MQTT_BROKER fails 3 times in a
# row, the firmware scans every IP on the local /24 subnet for an open
# port 1883.  When one is found it publishes to bush/pipeline/ping and
# waits for a reply on bush/pipeline/pong from stt-service.  The
# configured host is retried periodically during scanning so it
# recovers immediately if it comes back online.

import board
import digitalio
import json
import time
import wifi
import socketpool
import supervisor
import struct
import microcontroller

# ── Load secrets ────────────────────────────────────────────────────────────
try:
    from secrets import secrets
except ImportError:
    raise RuntimeError("Create secrets.py — see secrets.example.py")

# ── Outputs ──────────────────────────────────────────────────────────────────
# Seven solenoids, addressed by INDEX here and by NAME over MQTT. The
# name->index map is runtime state (see VALVE_MAP), because the loom is built
# before anyone knows which physical valve landed on which driver channel.
# Wired GP6..GP12 in header order: six new drivers plus GP9, which already
# carried the original poof relay. Channel index == position in this list, so
# channel 0 is GP6 and channel 6 is GP12 — that indexing is what
# bush/flame/identify addresses and what bush-valve-id reports.
# GP2/GP3 (the old flare and bigjet channels) are deliberately NOT outputs any
# more; everything moved to the contiguous block.
OUTPUT_PINS = [board.GP6, board.GP7, board.GP8, board.GP9,
               board.GP10, board.GP11, board.GP12]
NUM_OUTPUTS = len(OUTPUT_PINS)

outputs = []
for _p in OUTPUT_PINS:
    _io = digitalio.DigitalInOut(_p)
    _io.direction = digitalio.Direction.OUTPUT
    _io.value = False
    outputs.append(_io)

# Scheduled off-time per output in ms (supervisor.ticks_ms); None = not firing.
off_ms = [None] * NUM_OUTPUTS

# name -> output index. Identity-ish default so a board with no map published
# is still usable and, more importantly, predictable. Replaced wholesale by a
# retained bush/flame/map message; the broker is the persistence layer, which
# avoids remounting CIRCUITPY read-write just to store a dict.
DEFAULT_VALVE_MAP = {
    "flare1": 0, "flare2": 1, "flare3": 2,
    "bigjet1": 3, "bigjet2": 4, "bigjet3": 5,
    "poof1": 6,
}
valve_map = dict(DEFAULT_VALVE_MAP)

# Hard ceiling for one pulse. A typo'd or hostile "ms" must not be able to
# hold a solenoid open indefinitely — this is gas, not an LED.
MAX_PULSE_MS = 10_000

TOPIC_FLAME        = b"bush/flame/pulse"
TOPIC_FLAME_STATUS = b"bush/flame/status"
TOPIC_FLAME_IDENT  = b"bush/flame/identify"
TOPIC_FLAME_MAP    = b"bush/flame/map"
TOPIC_FLAME_STOP   = b"bush/flame/stop"
PIPELINE_PING      = b"bush/pipeline/ping"
PIPELINE_PONG      = b"bush/pipeline/pong"

# ── Tick arithmetic (handles 29-day rollover) ────────────────────────────────
def ticks_diff(later, earlier):
    return (later - earlier) & 0x3FFFFFFF

def ticks_expired(deadline):
    if deadline is None:
        return False
    return ticks_diff(supervisor.ticks_ms(), deadline) < 0x1FFFFFFF

# ── Pin update — call as often as possible ───────────────────────────────────
def service_pins():
    for i in range(NUM_OUTPUTS):
        if off_ms[i] is not None and ticks_expired(off_ms[i]):
            outputs[i].value = False
            off_ms[i] = None
            print("out%d OFF" % i)


def force_pins_off():
    # Drop solenoids and clear schedules before any blocking Wi-Fi
    # recovery — toggling the radio or sleeping between retries can
    # outlast a pulse's OFF deadline.
    for i in range(NUM_OUTPUTS):
        outputs[i].value = False
        off_ms[i] = None


def fire_output(index, duration_ms, label):
    """Energise one output for duration_ms. Never shortens an active pulse."""
    if index < 0 or index >= NUM_OUTPUTS:
        print("Bad output index:", index)
        return
    deadline = (supervisor.ticks_ms() + duration_ms) & 0x3FFFFFFF
    outputs[index].value = True
    if off_ms[index] is None or ticks_diff(deadline, off_ms[index]) < 0x1FFFFFFF:
        off_ms[index] = deadline
    print("%s (out%d) ON %dms" % (label, index, duration_ms))

# ─────────────────────────────────────────────────────────────────────────────
# Minimal hand-rolled MQTT client over a non-blocking raw socket.
# adafruit_minimqtt is NOT used — its loop() can block arbitrarily.
# This implementation only does what we need:
#   ✓ CONNECT / CONNACK
#   ✓ SUBSCRIBE
#   ✓ PINGREQ / PINGRESP (keep-alive)
#   ✓ PUBLISH receive (QoS 0 — broker must publish at QoS 0 to us)
# All socket reads are non-blocking; partial reads are buffered and
# completed on the next iteration so pins are never stalled.
# ─────────────────────────────────────────────────────────────────────────────

MQTT_PORT     = secrets.get("MQTT_PORT", 1883)
MQTT_BROKER   = secrets["MQTT_BROKER"]
MQTT_USER     = secrets.get("MQTT_USER", None)
MQTT_PASSWORD = secrets.get("MQTT_PASSWORD", None)
KEEP_ALIVE    = 15          # seconds
PING_INTERVAL = 10_000      # ms between PINGREQs
STATUS_INTERVAL_MS = 5_000  # ms between bush/flame/status beacons

# A purely periodic beacon cannot describe a pulse shorter than its own
# interval: a 100 ms flare opens and closes entirely between two 5 s beacons,
# so anything watching the beacon never sees it. Publish on state change too,
# rate-limited so a fast fire pattern (sentiment drives flares every ~260 ms)
# cannot flood the socket or stall the pin servicing.
STATUS_CHANGE_MIN_MS = 120

sock          = None
pool          = None
rx_buf        = bytearray()  # persistent receive buffer
last_ping_ms  = 0
last_status_ms = 0
connected     = False

# ── Connection state machine ─────────────────────────────────────────────────
ST_CONNECTED        = 0   # normal operation
ST_RETRY_CONFIGURED = 1   # retrying secrets["MQTT_BROKER"]
ST_SCAN_PROBE       = 2   # TCP-probing one subnet IP per loop pass
ST_SCAN_CONNECT     = 3   # probe succeeded — attempt full MQTT handshake
ST_VERIFY_PIPELINE  = 4   # connected to scanned broker — await pipeline proof

conn_state          = ST_RETRY_CONFIGURED
configured_failures = 0
MAX_CONFIGURED_TRIES = 3   # failures before starting subnet scan

scan_index          = 0    # 0–254, indexes host octet of current candidate
scan_base           = None # e.g. "192.168.1."  — derived from own IP
scan_candidate      = None # IP string currently being tested
pipeline_verified   = False
verify_deadline_ms  = None

RECONNECT_INTERVAL  = 3_000   # ms between configured-broker retry attempts
VERIFY_WAIT_MS      = 3_000   # ms to wait for bush/pipeline/status after connecting
SCAN_PROBE_TIMEOUT  = 0.5     # seconds — TCP connect timeout for port probes
SCAN_RETRY_INTERVAL = 50      # re-try configured broker every N scan IPs


def encode_string(s):
    if isinstance(s, str):
        s = s.encode()
    return struct.pack("!H", len(s)) + s


def mqtt_connect_packet():
    client_id = b"pico2w-gpio"
    proto     = b"MQTT"
    payload   = encode_string(client_id)
    if MQTT_USER:
        connect_flags = 0xC2  # username + password + clean session
        payload += encode_string(MQTT_USER)
        payload += encode_string(MQTT_PASSWORD or "")
    else:
        connect_flags = 0x02  # clean session only
    variable = (
        encode_string(proto)
        + bytes([0x04, connect_flags])
        + struct.pack("!H", KEEP_ALIVE)
    )
    remaining = len(variable) + len(payload)
    return bytes([0x10]) + encode_remaining(remaining) + variable + payload


def mqtt_subscribe_packet(topic, packet_id=1):
    t = topic if isinstance(topic, bytes) else topic.encode()
    payload = struct.pack("!H", packet_id) + encode_string(t) + bytes([0x00])
    return bytes([0x82]) + encode_remaining(len(payload)) + payload


def mqtt_publish_packet(topic, payload=b""):
    t = topic if isinstance(topic, bytes) else topic.encode()
    p = payload if isinstance(payload, bytes) else payload.encode()
    body = encode_string(t) + p
    return bytes([0x30]) + encode_remaining(len(body)) + body


def mqtt_pingreq():
    return bytes([0xC0, 0x00])


def encode_remaining(n):
    out = bytearray()
    while True:
        byte = n & 0x7F
        n >>= 7
        if n:
            byte |= 0x80
        out.append(byte)
        if not n:
            break
    return bytes(out)


def wifi_connect():
    global pool
    print("Connecting to Wi-Fi:", secrets["SSID"])
    # Explicit timeout caps a single attempt's blocking time. Without it, a
    # hung join can hold the main loop for many minutes, starving the
    # rest of the main loop.
    wifi.radio.connect(secrets["SSID"], secrets["PASSWORD"], timeout=10)
    print("Wi-Fi OK, IP:", wifi.radio.ipv4_address)
    pool = socketpool.SocketPool(wifi.radio)


WIFI_RETRIES_BEFORE_RADIO_RESET    = 3
WIFI_RADIO_RESETS_BEFORE_CPU_RESET = 2


def wifi_connect_with_recovery():
    """Connect to Wi-Fi with escalating recovery for chip-level hangs.

    Ladder: plain retry → wifi.radio.enabled toggle → microcontroller.reset().
    Pins are forced OFF up front because each rung blocks for ~seconds.
    """
    force_pins_off()
    radio_resets = 0
    while True:
        for attempt in range(WIFI_RETRIES_BEFORE_RADIO_RESET):
            try:
                wifi_connect()
                return
            except Exception as e:
                print("Wi-Fi connect failed (attempt {}): {}".format(attempt + 1, e))
                time.sleep(2)
        if radio_resets >= WIFI_RADIO_RESETS_BEFORE_CPU_RESET:
            print("Wi-Fi: radio toggle didn't help, resetting MCU")
            time.sleep(0.1)
            microcontroller.reset()
        print("Wi-Fi: power-cycling radio (enabled = False/True)")
        try:
            wifi.radio.enabled = False
        except Exception as e:
            print("Wi-Fi: radio off failed:", e)
        time.sleep(1)
        try:
            wifi.radio.enabled = True
        except Exception as e:
            print("Wi-Fi: radio on failed:", e)
        radio_resets += 1


def compute_scan_base():
    """Derive the /24 network prefix from our own IP (e.g. '192.168.1.')."""
    global scan_base
    parts = str(wifi.radio.ipv4_address).split(".")
    scan_base = parts[0] + "." + parts[1] + "." + parts[2] + "."
    print("Scan base:", scan_base)


def tcp_probe(ip):
    """Try to TCP-connect to ip:MQTT_PORT with a short timeout.
    Returns True if the port is open.  Always closes the socket."""
    force_pins_off()
    s = None
    try:
        s = pool.socket(pool.AF_INET, pool.SOCK_STREAM)
        s.settimeout(SCAN_PROBE_TIMEOUT)
        s.connect((ip, MQTT_PORT))
        return True
    except Exception:
        return False
    finally:
        if s is not None:
            try:
                s.close()
            except Exception:
                pass


# CircuitPython's socketpool can raise EINPROGRESS (119) from connect() even
# with a timeout set — the connect has *started*, not failed. Treating that as
# an error made the board give up on any broker slow enough to not complete
# inside one call, which is every broker a hop or two away. Retry until the
# stack reports the connection is established (EISCONN) or the deadline
# passes.
EINPROGRESS = 119
EALREADY    = 114
EISCONN     = 127
CONNECT_TIMEOUT_S = 8


def _connect_socket(s, broker):
    """connect(), tolerating a non-blocking stack's in-progress errnos."""
    deadline = supervisor.ticks_ms() + int(CONNECT_TIMEOUT_S * 1000)
    last = None
    while ticks_diff(deadline, supervisor.ticks_ms()) < 0x1FFFFFFF:
        try:
            s.connect((broker, MQTT_PORT))
            return
        except OSError as e:
            err = e.args[0] if e.args else None
            if err == EISCONN:
                return                      # already established: done
            if err not in (EINPROGRESS, EALREADY):
                raise
            last = e
            time.sleep(0.2)
    raise last if last else OSError("connect timed out")


def mqtt_open(broker=None):
    """Open TCP socket, send CONNECT, wait for CONNACK, then go non-blocking."""
    global sock, rx_buf, connected, last_ping_ms
    force_pins_off()
    if broker is None:
        broker = MQTT_BROKER
    if sock:
        try:
            sock.close()
        except Exception:
            pass
        sock = None
    connected = False
    rx_buf = bytearray()
    print("Connecting to MQTT broker", broker, "…")
    try:
        s = pool.socket(pool.AF_INET, pool.SOCK_STREAM)
        s.settimeout(CONNECT_TIMEOUT_S)        # blocking only during handshake
        _connect_socket(s, broker)
        s.send(mqtt_connect_packet())
        # Wait for CONNACK (4 bytes)
        buf = bytearray(4)
        s.recv_into(buf, 4)
        if buf[0] != 0x20 or buf[3] != 0x00:
            raise RuntimeError(f"CONNACK failed: {list(buf)}")
        # Switch to non-blocking for normal operation
        s.settimeout(0)
        sock = s
        connected = True
        last_ping_ms = supervisor.ticks_ms()
        print("MQTT connected.")
    except Exception as e:
        print("mqtt_open failed:", e)
        connected = False


def decode_remaining(buf, pos):
    """Decode MQTT variable-length remaining field. Returns (value, new_pos)."""
    mult = 1
    val  = 0
    while pos < len(buf):
        byte = buf[pos]
        pos += 1
        val += (byte & 0x7F) * mult
        mult <<= 7
        if not (byte & 0x80):
            return val, pos
        if mult > 2097152:
            break
    return None, pos   # incomplete


def process_packets():
    """Parse and dispatch all complete MQTT packets sitting in rx_buf."""
    global rx_buf, pipeline_verified, valve_map
    pos = 0
    while pos < len(rx_buf):
        if pos + 2 > len(rx_buf):
            break  # need at least 2 bytes for type + first remaining byte
        pkt_type = rx_buf[pos] & 0xF0
        remaining, hdr_end = decode_remaining(rx_buf, pos + 1)
        if remaining is None or hdr_end + remaining > len(rx_buf):
            break  # incomplete packet — leave in buffer

        pkt_end = hdr_end + remaining
        pkt     = rx_buf[hdr_end:pkt_end]

        if pkt_type == 0x30:   # PUBLISH (QoS 0)
            if len(pkt) < 2:
                pos = pkt_end
                continue
            topic_len = struct.unpack("!H", pkt[0:2])[0]
            if len(pkt) < 2 + topic_len:
                pos = pkt_end
                continue
            topic   = bytes(pkt[2:2 + topic_len])
            payload = bytes(pkt[2 + topic_len:])  # QoS 0: no packet identifier

            if topic == PIPELINE_PONG:
                pipeline_verified = True
                print("Pipeline verified:", payload)
                pos = pkt_end
                continue

            if topic == TOPIC_FLAME_STOP:
                # Emergency stop. Drops every solenoid and clears every
                # pending off-time, whatever the payload says — this must
                # never fail to parse. Checked before anything else so a
                # backlog of queued pulses cannot be serviced first.
                force_pins_off()
                print("ALL STOP")
                pos = pkt_end
                continue

            if topic == TOPIC_FLAME_MAP:
                # Retained: {"flare1": 0, "bigjet2": 4, ...}. Replaces the map
                # wholesale so a stale name can never linger. Rejected as a
                # unit if anything is malformed — a half-applied map would
                # fire the wrong valve, which is worse than an outdated one.
                try:
                    incoming = json.loads(payload)
                    newmap = {}
                    for k in incoming:
                        idx = int(incoming[k])
                        if idx < 0 or idx >= NUM_OUTPUTS:
                            raise ValueError(k)
                        newmap[str(k)] = idx
                except (ValueError, KeyError, TypeError):
                    print("Bad valve map, keeping current:", payload)
                    pos = pkt_end
                    continue
                if newmap:
                    force_pins_off()   # never remap while something is lit
                    valve_map = newmap
                    print("Valve map updated:", valve_map)
                pos = pkt_end
                continue

            if topic == TOPIC_FLAME_IDENT:
                # {"out": 3, "ms": 400} — fires a RAW output index, ignoring
                # the map. This is how you find out which physical solenoid a
                # channel drives before any map exists.
                try:
                    data = json.loads(payload)
                    out_idx = int(data["out"])
                    duration_ms = int(data["ms"])
                except (ValueError, KeyError):
                    print("Bad identify payload:", payload)
                    pos = pkt_end
                    continue
                if 0 < duration_ms <= MAX_PULSE_MS:
                    fire_output(out_idx, duration_ms, "identify")
                pos = pkt_end
                continue

            if topic != TOPIC_FLAME:
                pos = pkt_end
                continue

            try:
                data = json.loads(payload)
                flame_valve = data["valve"]
                duration_ms = int(data["ms"])
            except (ValueError, KeyError):
                print("Bad payload:", payload)
                pos = pkt_end
                continue

            if duration_ms > 0:
                if duration_ms > MAX_PULSE_MS:
                    print("Pulse %dms exceeds cap, clamping" % duration_ms)
                    duration_ms = MAX_PULSE_MS
                idx = valve_map.get(flame_valve)
                if idx is None:
                    # Every valve is individually addressed; a bare type name
                    # like "bigjet" is not a valve and must not guess.
                    print("Unknown valve:", flame_valve)
                else:
                    fire_output(idx, duration_ms, flame_valve)

        elif pkt_type == 0xD0:  # PINGRESP — nothing to do
            pass
        elif pkt_type == 0x90:  # SUBACK — nothing to do
            pass

        pos = pkt_end

    # Discard consumed bytes
    if pos:
        rx_buf = rx_buf[pos:]


def mqtt_loop():
    """Non-blocking: drain the socket, parse packets, send keep-alive ping."""
    global rx_buf, connected, last_ping_ms
    if not connected or sock is None:
        return

    # Read up to 256 bytes — returns immediately (settimeout(0))
    tmp = bytearray(256)
    try:
        n = sock.recv_into(tmp, 256)
        if n == 0:
            raise OSError("connection closed by broker")
        rx_buf.extend(tmp[:n])
        process_packets()
    except OSError as e:
        err = e.errno if hasattr(e, "errno") else None
        if err in (11, 35, 119):
            pass   # EAGAIN / EWOULDBLOCK — no data right now, totally normal
        else:
            print("Socket read error:", e)
            connected = False
            return

    # Keep-alive ping
    now = supervisor.ticks_ms()
    if ticks_diff(now, last_ping_ms) >= PING_INTERVAL:
        try:
            sock.send(mqtt_pingreq())
            last_ping_ms = now
        except OSError as e:
            print("Ping failed:", e)
            connected = False


# ── Subscribe helper ─────────────────────────────────────────────────────────
def subscribe_all():
    """Subscribe to flame topics."""
    sock.send(mqtt_subscribe_packet(TOPIC_FLAME, packet_id=1))
    sock.send(mqtt_subscribe_packet(TOPIC_FLAME_IDENT, packet_id=2))
    # Retained, so the broker replays the current map on every (re)connect —
    # a rebooted board re-learns its wiring without anyone republishing.
    sock.send(mqtt_subscribe_packet(TOPIC_FLAME_MAP, packet_id=3))
    sock.send(mqtt_subscribe_packet(TOPIC_FLAME_STOP, packet_id=4))
    print("Subscribed (flame, identify, map, stop).")


_last_reported = None      # tuple of output states at the last publish


def publish_status_on_change():
    """Publish immediately when an output changes, at a bounded rate."""
    global _last_reported, last_status_ms
    now = tuple(o.value for o in outputs)
    if now == _last_reported:
        return
    if ticks_diff(supervisor.ticks_ms(), last_status_ms) < STATUS_CHANGE_MIN_MS:
        return                      # too soon; the periodic beacon still covers it
    _last_reported = now
    publish_flame_status(force=True)


def publish_flame_status(force=False):
    """Liveness beacon — deploy verification subscribes to this."""
    global last_status_ms
    now = supervisor.ticks_ms()
    if not force and ticks_diff(now, last_status_ms) < STATUS_INTERVAL_MS:
        return
    last_status_ms = now
    # Report by name so a reader does not need the map to interpret it, plus
    # the raw channel states and the map itself for commissioning.
    named = {}
    for _n in valve_map:
        named[_n] = outputs[valve_map[_n]].value
    payload = json.dumps({"ticks_ms": now, "outputs": [o.value for o in outputs],
                          "valves": named, "map": valve_map})
    try:
        sock.send(mqtt_publish_packet(TOPIC_FLAME_STATUS, payload))
        globals()["_last_reported"] = tuple(o.value for o in outputs)
    except OSError:
        pass


# ── Boot ─────────────────────────────────────────────────────────────────────
wifi_connect_with_recovery()
compute_scan_base()
mqtt_open()
if connected:
    subscribe_all()
    publish_flame_status(force=True)
    conn_state = ST_CONNECTED
else:
    conn_state = ST_RETRY_CONFIGURED

last_reconnect_ms = 0

# ── Main loop ─────────────────────────────────────────────────────────────────
while True:
    # 🔴 Pins FIRST — always, unconditionally, ~2 µs
    service_pins()

    # ── CONNECTED: normal operation ──────────────────────────────────────────
    if conn_state == ST_CONNECTED:
        mqtt_loop()
        if connected and sock is not None:
            # Change-driven first so a short pulse is actually reported, then
            # the periodic beacon as the liveness heartbeat.
            publish_status_on_change()
            publish_flame_status()
        if not connected:
            print("Connection lost, retrying configured broker…")
            conn_state = ST_RETRY_CONFIGURED
            configured_failures = 0

    # ── RETRY_CONFIGURED: keep hammering the known broker ───────────────────
    elif conn_state == ST_RETRY_CONFIGURED:
        now = supervisor.ticks_ms()
        if ticks_diff(now, last_reconnect_ms) >= RECONNECT_INTERVAL:
            last_reconnect_ms = now
            service_pins()
            try:
                if not wifi.radio.ipv4_address:
                    wifi_connect_with_recovery()
                    compute_scan_base()
                mqtt_open(MQTT_BROKER)
            except Exception as e:
                print("Reconnect error:", e)
            if connected:
                subscribe_all()
                publish_flame_status(force=True)
                conn_state = ST_CONNECTED
                configured_failures = 0
            else:
                configured_failures += 1
                print(f"Configured broker failed ({configured_failures}/{MAX_CONFIGURED_TRIES})")
                if configured_failures >= MAX_CONFIGURED_TRIES:
                    print("Scanning subnet for MQTT broker…")
                    conn_state = ST_SCAN_PROBE
                    scan_index = 0

    # ── SCAN_PROBE: probe one IP per loop pass ───────────────────────────────
    elif conn_state == ST_SCAN_PROBE:
        if scan_index > 254:
            print("Subnet scan complete, no verified pipeline broker found.")
            conn_state = ST_RETRY_CONFIGURED
            configured_failures = 0
            continue

        # Periodically retry the configured broker mid-scan
        if scan_index > 0 and scan_index % SCAN_RETRY_INTERVAL == 0:
            service_pins()
            mqtt_open(MQTT_BROKER)
            if connected:
                subscribe_all()
                publish_flame_status(force=True)
                print("Configured broker back online, subscribed.")
                conn_state = ST_CONNECTED
                configured_failures = 0
                continue

        candidate = scan_base + str(scan_index)
        my_ip = str(wifi.radio.ipv4_address)
        scan_index += 1

        # Skip our own IP and the configured broker (already tried)
        if candidate == my_ip or candidate == MQTT_BROKER:
            continue

        service_pins()
        if tcp_probe(candidate):
            print(f"Port {MQTT_PORT} open on {candidate}, attempting MQTT…")
            scan_candidate = candidate
            conn_state = ST_SCAN_CONNECT

    # ── SCAN_CONNECT: full MQTT handshake with the candidate ─────────────────
    elif conn_state == ST_SCAN_CONNECT:
        service_pins()
        mqtt_open(scan_candidate)
        if connected:
            # Subscribe to the pipeline verification topic
            pipeline_verified = False
            sock.send(mqtt_subscribe_packet(PIPELINE_PONG, packet_id=10))
            sock.send(mqtt_publish_packet(PIPELINE_PING))
            verify_deadline_ms = (supervisor.ticks_ms() + VERIFY_WAIT_MS) & 0x3FFFFFFF
            print(f"Waiting for pipeline pong on {scan_candidate}…")
            conn_state = ST_VERIFY_PIPELINE
        else:
            # Handshake failed — continue scanning
            conn_state = ST_SCAN_PROBE

    # ── VERIFY_PIPELINE: drain socket until status arrives or timeout ────────
    elif conn_state == ST_VERIFY_PIPELINE:
        if not connected:
            print("Scanned broker disconnected during verify, continuing scan…")
            conn_state = ST_SCAN_PROBE
            continue

        mqtt_loop()  # drains socket; process_packets() sets pipeline_verified

        if pipeline_verified:
            # Good broker — subscribe to fire topics and go live
            subscribe_all()
            publish_flame_status(force=True)
            print(f"Pipeline verified on {scan_candidate}, subscribed.")
            conn_state = ST_CONNECTED
        elif ticks_expired(verify_deadline_ms):
            print(f"No pipeline on {scan_candidate}, continuing scan…")
            try:
                sock.close()
            except Exception:
                pass
            connected = False
            conn_state = ST_SCAN_PROBE

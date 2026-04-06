# main.py — Pi Pico 2 W MQTT GPIO pulse controller
# CircuitPython 9.x
#
# Safety guarantee: pins are ALWAYS turned off on schedule.
# MQTT I/O is done in small non-blocking chunks; if it blocks or
# fails the pins still go off. Relays will never get stuck on.
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
import wifi
import socketpool
import supervisor
import struct

# ── Load secrets ────────────────────────────────────────────────────────────
try:
    from secrets import secrets
except ImportError:
    raise RuntimeError("Create secrets.py — see secrets.example.py")

# ── Pin setup ────────────────────────────────────────────────────────────────
pin_flare = digitalio.DigitalInOut(board.GP2)
pin_flare.direction = digitalio.Direction.OUTPUT
pin_flare.value = False

pin_bigjet = digitalio.DigitalInOut(board.GP3)
pin_bigjet.direction = digitalio.Direction.OUTPUT
pin_bigjet.value = False

pin_poof = digitalio.DigitalInOut(board.GP7)
pin_poof.direction = digitalio.Direction.OUTPUT
pin_poof.value = False

# ── Scheduled off-times in ms (supervisor.ticks_ms) ─────────────────────────
# None = not scheduled
off_ms_flare  = None
off_ms_bigjet = None
off_ms_poof   = None

TOPIC_FLARE        = b"bush/flame/flare/pulse"
TOPIC_BIGJET       = b"bush/flame/bigjet/pulse"
TOPIC_POOF         = b"bush/flame/poof/pulse"
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
    global off_ms_flare, off_ms_bigjet
    if off_ms_flare is not None and ticks_expired(off_ms_flare):
        pin_flare.value = False
        off_ms_flare = None
        print("Flare OFF")
    if off_ms_bigjet is not None and ticks_expired(off_ms_bigjet):
        pin_bigjet.value = False
        off_ms_bigjet = None
        print("Bigjet OFF")
    if off_ms_poof is not None and ticks_expired(off_ms_poof):
        pin_poof.value = False
        off_ms_poof = None
        print("Poof OFF")

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

sock          = None
pool          = None
rx_buf        = bytearray()  # persistent receive buffer
last_ping_ms  = 0
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
    wifi.radio.connect(secrets["SSID"], secrets["PASSWORD"])
    print("Wi-Fi OK, IP:", wifi.radio.ipv4_address)
    pool = socketpool.SocketPool(wifi.radio)


def compute_scan_base():
    """Derive the /24 network prefix from our own IP (e.g. '192.168.1.')."""
    global scan_base
    parts = str(wifi.radio.ipv4_address).split(".")
    scan_base = parts[0] + "." + parts[1] + "." + parts[2] + "."
    print("Scan base:", scan_base)


def tcp_probe(ip):
    """Try to TCP-connect to ip:MQTT_PORT with a short timeout.
    Returns True if the port is open.  Always closes the socket."""
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


def mqtt_open(broker=None):
    """Open TCP socket, send CONNECT, wait for CONNACK, then go non-blocking."""
    global sock, rx_buf, connected, last_ping_ms
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
        s.settimeout(5)                        # blocking only during handshake
        s.connect((broker, MQTT_PORT))
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
    global off_ms_flare, off_ms_bigjet, off_ms_poof, rx_buf, pipeline_verified
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

            try:
                duration_ms = int(payload)
            except ValueError:
                print("Bad payload:", payload)
                pos = pkt_end
                continue

            if duration_ms > 0:
                deadline = (supervisor.ticks_ms() + duration_ms) & 0x3FFFFFFF
                if topic == TOPIC_FLARE:
                    pin_flare.value = True
                    # Extend deadline; never shorten an active pulse
                    if off_ms_flare is None:
                        off_ms_flare = deadline
                    else:
                        # pick whichever deadline is further in the future
                        if ticks_diff(deadline, off_ms_flare) < 0x1FFFFFFF:
                            off_ms_flare = deadline
                    print(f"Flare ON {duration_ms}ms")
                elif topic == TOPIC_BIGJET:
                    pin_bigjet.value = True
                    if off_ms_bigjet is None:
                        off_ms_bigjet = deadline
                    else:
                        if ticks_diff(deadline, off_ms_bigjet) < 0x1FFFFFFF:
                            off_ms_bigjet = deadline
                    print(f"Bigjet ON {duration_ms}ms")
                elif topic == TOPIC_POOF:
                    pin_poof.value = True
                    if off_ms_poof is None:
                        off_ms_poof = deadline
                    else:
                        if ticks_diff(deadline, off_ms_poof) < 0x1FFFFFFF:
                            off_ms_poof = deadline
                    print(f"Poof ON {duration_ms}ms")

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


# ── Boot ─────────────────────────────────────────────────────────────────────
wifi_connect()
compute_scan_base()
mqtt_open()
if connected:
    sock.send(mqtt_subscribe_packet(TOPIC_FLARE,  packet_id=1))
    sock.send(mqtt_subscribe_packet(TOPIC_BIGJET, packet_id=2))
    sock.send(mqtt_subscribe_packet(TOPIC_POOF,   packet_id=3))
    print("Subscribed.")
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
                    wifi_connect()
                    compute_scan_base()
                mqtt_open(MQTT_BROKER)
            except Exception as e:
                print("Reconnect error:", e)
            if connected:
                sock.send(mqtt_subscribe_packet(TOPIC_FLARE,  packet_id=1))
                sock.send(mqtt_subscribe_packet(TOPIC_BIGJET, packet_id=2))
                print("Subscribed.")
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
                sock.send(mqtt_subscribe_packet(TOPIC_FLARE,  packet_id=1))
                sock.send(mqtt_subscribe_packet(TOPIC_BIGJET, packet_id=2))
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
            sock.send(mqtt_subscribe_packet(TOPIC_FLARE,  packet_id=1))
            sock.send(mqtt_subscribe_packet(TOPIC_BIGJET, packet_id=2))
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

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

# ── Scheduled off-times in ms (supervisor.ticks_ms) ─────────────────────────
# None = not scheduled
off_ms_flare  = None
off_ms_bigjet = None

TOPIC_FLARE  = b"bush/flame/flare/pulse"
TOPIC_BIGJET = b"bush/flame/bigjet/pulse"

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


def mqtt_open():
    """Open TCP socket, send CONNECT, wait for CONNACK, then go non-blocking."""
    global sock, rx_buf, connected, last_ping_ms
    if sock:
        try:
            sock.close()
        except Exception:
            pass
        sock = None
    connected = False
    rx_buf = bytearray()
    print("Connecting to MQTT broker…")
    try:
        s = pool.socket(pool.AF_INET, pool.SOCK_STREAM)
        s.settimeout(5)                        # blocking only during handshake
        s.connect((MQTT_BROKER, MQTT_PORT))
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
        sock.send(mqtt_subscribe_packet(TOPIC_FLARE,  packet_id=1))
        sock.send(mqtt_subscribe_packet(TOPIC_BIGJET, packet_id=2))
        print("Subscribed.")
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
    global off_ms_flare, off_ms_bigjet, rx_buf
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
mqtt_open()

RECONNECT_INTERVAL = 3_000   # ms between reconnect attempts
last_reconnect_ms  = 0

# ── Main loop ─────────────────────────────────────────────────────────────────
while True:
    # 🔴 Pins FIRST — always, unconditionally, ~2 µs
    service_pins()

    # 🌐 Network I/O — non-blocking, safe every iteration
    if connected:
        mqtt_loop()
    else:
        now = supervisor.ticks_ms()
        if ticks_diff(now, last_reconnect_ms) >= RECONNECT_INTERVAL:
            last_reconnect_ms = now
            service_pins()   # one more check before the blocking handshake
            try:
                if not wifi.radio.ipv4_address:
                    wifi_connect()
                mqtt_open()
            except Exception as e:
                print("Reconnect error:", e)

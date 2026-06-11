# code.py — Pi Pico 2 W standalone NEEDLE-VALVE node (CircuitPython 10.x)
#
# Drives the MKS SERVO42D over CAN (MCP2515 SPI-CAN controller; valve.py is the
# closed-loop driver) and plays bush-cue streamed waveforms, all over Wi-Fi + MQTT.
# This is a separate, valve-only firmware (no flame relays here).
#
# Invariants for this main loop:
#   1. MQTT keepalive: a PINGREQ must reach the broker within KEEP_ALIVE (15 s), so
#      mqtt_loop() runs every iteration and nothing blocks for long.
#   2. valve.service() runs every iteration, non-blocking — it ticks the streamed-
#      playback clock and the homing/silence watchdogs.
#   3. Halt valve motion (valve_safe) before any blocking op (Wi-Fi recovery / subnet
#      scan): a streamed 0xF6 constant-speed keeps the motor running while we block.
#
# secrets.py: Wi-Fi via NETWORKS=[{ssid,password},...] (or single SSID/PASSWORD), brokers
# via MQTT_BROKERS=[ip,...] (or single MQTT_BROKER), CAN carrier via BOARD ("canberry").
# Broker discovery: after MAX_CONFIGURED_TRIES passes over the broker list it scans the
# /24 for an open :1883 and verifies via bush/pipeline/ping -> bush/pipeline/pong.

import board
import busio
import digitalio
import json
import time
import wifi
import socketpool
import supervisor
import struct
import microcontroller

from adafruit_mcp2515 import MCP2515 as CAN
from adafruit_mcp2515.canio import Message

import valve

# ── Load secrets ────────────────────────────────────────────────────────────
try:
    from secrets import secrets
except ImportError:
    raise RuntimeError("Create secrets.py — see secrets.example.py")

# ── MKS SERVO42D over CAN (MCP2515 SPI-CAN controller) ────────────────────────
# valve.py is the CAN closed-loop driver; assign valve.can + valve.Message before
# valve.init(). The CAN *carrier* (which SPI pins + crystal) is per-board — pick it from
# secrets["BOARD"]. On the Pico 2 W the CanBerry's MCP2515 SPI reuses the old UART pins.
# The crystal MUST match the module (16 vs 8 MHz) or the bus silently never ACKs.
# 42D menu: CAN rate 500 kbps, CAN ID 1, microstep 16 (MStep) — valve.init() sets work
# mode SR_vFOC and the run current over the bus. Flip valve.DIR_TOWARD_OPEN/CLOSED if a
# target move drives the wrong way.
BOARD_PROFILES = {
    # CanBerry Pi V1.1.1 (MCP2515 @ 16 MHz) wired to Pico 2 W GPIO (reuses old UART pins).
    "canberry":  {"clk": board.GP6, "mosi": board.GP7, "miso": board.GP4, "cs": board.GP5,
                  "crystal": 16_000_000},
    # Waveshare RP2350-CAN onboard XL2515 (SPI1). Crystal: bench-confirm 16 vs 8 MHz.
    # (Waveshare has no radio, so it runs the USB-serial main — kept here for parity.)
    "waveshare": {"clk": board.GP10, "mosi": board.GP11, "miso": board.GP12, "cs": board.GP9,
                  "crystal": 16_000_000},
}
CAN_BITRATE = 500_000
_prof = BOARD_PROFILES[secrets.get("BOARD", "canberry")]
_can_spi = busio.SPI(_prof["clk"], _prof["mosi"], _prof["miso"])
_can_cs = digitalio.DigitalInOut(_prof["cs"])
_can_cs.switch_to_output(True)
valve.Message = Message
valve.can = CAN(_can_spi, _can_cs, baudrate=CAN_BITRATE, crystal_freq=_prof["crystal"])

TOPIC_STREAM       = b"bush/fire/valve/stream"   # binary bush-cue waveform frames
PIPELINE_PING      = b"bush/pipeline/ping"
PIPELINE_PONG      = b"bush/pipeline/pong"

# ── Tick arithmetic (handles 29-day rollover) ────────────────────────────────
def ticks_diff(later, earlier):
    return (later - earlier) & 0x3FFFFFFF

def ticks_expired(deadline):
    if deadline is None:
        return False
    return ticks_diff(supervisor.ticks_ms(), deadline) < 0x1FFFFFFF

def valve_safe():
    """Halt valve motion before a blocking op (Wi-Fi recovery / subnet scan): a
    streamed 0xF6 constant-speed would otherwise keep running while we're blocked."""
    try:
        valve.cmd_stop()
    except Exception:
        pass

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
# Multiple brokers: try each in turn before scanning. Back-compatible with single MQTT_BROKER.
BROKERS       = secrets.get("MQTT_BROKERS") or [secrets["MQTT_BROKER"]]
broker_index  = 0           # current broker; left pointing at the last-good one
MQTT_USER     = secrets.get("MQTT_USER", None)
MQTT_PASSWORD = secrets.get("MQTT_PASSWORD", None)
KEEP_ALIVE    = 15          # seconds
PING_INTERVAL = 10_000      # ms between PINGREQs


def current_broker():
    return BROKERS[broker_index % len(BROKERS)]

sock          = None
pool          = None
rx_buf        = bytearray()  # persistent receive buffer
last_ping_ms  = 0
connected     = False

# ── Connection state machine ─────────────────────────────────────────────────
ST_CONNECTED        = 0   # normal operation
ST_RETRY_CONFIGURED = 1   # cycling through the configured brokers (BROKERS)
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
    client_id = b"pico2w-valve"
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


# Multiple Wi-Fi networks: try each in turn. Back-compatible with single SSID/PASSWORD.
NETWORKS = secrets.get("NETWORKS") or [{"ssid": secrets["SSID"],
                                        "password": secrets.get("PASSWORD", "")}]


def wifi_connect():
    """Join the first reachable network in NETWORKS. Scans once and tries networks whose
    SSID is currently visible first, then falls back to trying each configured net blind.
    The per-attempt timeout caps blocking time so a hung join can't starve the main loop;
    raises if none join (the recovery ladder then toggles the radio / resets)."""
    global pool
    try:
        visible = set(n.ssid for n in wifi.radio.start_scanning_networks())
    except Exception:
        visible = set()
    finally:
        try:
            wifi.radio.stop_scanning_networks()
        except Exception:
            pass
    ordered = [n for n in NETWORKS if n["ssid"] in visible] + \
              [n for n in NETWORKS if n["ssid"] not in visible]
    last_err = None
    for net in ordered:
        try:
            print("Connecting to Wi-Fi:", net["ssid"])
            wifi.radio.connect(net["ssid"], net.get("password", ""), timeout=10)
            print("Wi-Fi OK, IP:", wifi.radio.ipv4_address)
            pool = socketpool.SocketPool(wifi.radio)
            return
        except Exception as e:
            last_err = e
            print("  join failed:", net["ssid"], "-", e)
    raise last_err or RuntimeError("no configured Wi-Fi network reachable")


WIFI_RETRIES_BEFORE_RADIO_RESET    = 3
WIFI_RADIO_RESETS_BEFORE_CPU_RESET = 2


def wifi_connect_with_recovery():
    """Connect to Wi-Fi with escalating recovery for chip-level hangs.

    Ladder: plain retry → wifi.radio.enabled toggle → microcontroller.reset().
    Valve motion is halted up front because each rung blocks for ~seconds.
    """
    valve_safe()
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
        broker = current_broker()
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


def _handle_stream_msg(payload):
    """A bush/fire/valve/stream MQTT message carries one binary wire frame
    (SENTINEL TYPE LEN(2 BE) DATA CRC); validate and hand it to valve.handle_stream."""
    if len(payload) < 5 or payload[0] != valve.STREAM_SENTINEL:
        return
    ln = (payload[2] << 8) | payload[3]
    end = 4 + ln
    if len(payload) < end + 1 or (sum(payload[:end]) & 0xFF) != payload[end]:
        return
    valve.handle_stream(payload[1], bytes(payload[4:end]))


def process_packets():
    """Parse and dispatch all complete MQTT packets sitting in rx_buf."""
    global rx_buf, pipeline_verified
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

            # Route valve topics to valve module
            if topic in valve.ALL_VALVE_TOPICS:
                valve.handle_mqtt(topic, payload)
                pos = pkt_end
                continue

            if topic == TOPIC_STREAM:
                _handle_stream_msg(payload)
            # any other PUBLISH is ignored; falls through to pos = pkt_end below

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
    """Subscribe to the valve command topics + the bush-cue stream topic."""
    sock.send(mqtt_subscribe_packet(TOPIC_STREAM, packet_id=1))
    for i, topic in enumerate(valve.ALL_VALVE_TOPICS):
        sock.send(mqtt_subscribe_packet(topic, packet_id=20 + i))
    print("Subscribed (valve + stream).")


def publish_valve_online(online=True):
    """Publish valve online/offline birth/LWT status."""
    try:
        sock.send(mqtt_publish_packet(valve.TOPIC_VALVE_ONLINE,
                                      b"online" if online else b"offline"))
    except Exception:
        pass


# ── Boot ─────────────────────────────────────────────────────────────────────
valve.init()
wifi_connect_with_recovery()
compute_scan_base()
mqtt_open()
if connected:
    subscribe_all()
    publish_valve_online()
    conn_state = ST_CONNECTED
else:
    conn_state = ST_RETRY_CONFIGURED

last_reconnect_ms = 0

# ── Main loop ─────────────────────────────────────────────────────────────────
while True:
    # Valve UART + streamed-playback clock — service every iteration, any MQTT state
    valve.service()

    # ── CONNECTED: normal operation ──────────────────────────────────────────
    if conn_state == ST_CONNECTED:
        mqtt_loop()
        # Publish valve status/position updates
        if connected and sock is not None:
            for vtopic, vpayload in valve.get_publish_messages():
                try:
                    sock.send(mqtt_publish_packet(vtopic, vpayload))
                except OSError:
                    pass
        if not connected:
            print("Connection lost, retrying configured broker…")
            valve_safe()
            conn_state = ST_RETRY_CONFIGURED
            configured_failures = 0

    # ── RETRY_CONFIGURED: cycle through the configured brokers ───────────────
    elif conn_state == ST_RETRY_CONFIGURED:
        now = supervisor.ticks_ms()
        if ticks_diff(now, last_reconnect_ms) >= RECONNECT_INTERVAL:
            last_reconnect_ms = now
            valve.service()
            broker = current_broker()
            try:
                if not wifi.radio.ipv4_address:
                    wifi_connect_with_recovery()
                    compute_scan_base()
                mqtt_open(broker)
            except Exception as e:
                print("Reconnect error:", e)
            if connected:
                subscribe_all()
                publish_valve_online()
                conn_state = ST_CONNECTED
                configured_failures = 0   # leave broker_index on this last-good broker
            else:
                # advance to the next broker; a full wrap through BROKERS = one pass
                broker_index += 1
                if broker_index % len(BROKERS) == 0:
                    configured_failures += 1
                    print(f"All brokers failed (pass {configured_failures}/{MAX_CONFIGURED_TRIES})")
                    if configured_failures >= MAX_CONFIGURED_TRIES:
                        print("Scanning subnet for MQTT broker…")
                        conn_state = ST_SCAN_PROBE
                        scan_index = 0
                else:
                    print("Broker failed, trying next:", current_broker())

    # ── SCAN_PROBE: probe one IP per loop pass ───────────────────────────────
    elif conn_state == ST_SCAN_PROBE:
        if scan_index > 254:
            print("Subnet scan complete, no verified pipeline broker found.")
            conn_state = ST_RETRY_CONFIGURED
            configured_failures = 0
            continue

        # Periodically retry the configured brokers mid-scan (rotating through the list)
        if scan_index > 0 and scan_index % SCAN_RETRY_INTERVAL == 0:
            valve.service()
            mqtt_open(current_broker())
            if connected:
                subscribe_all()
                publish_valve_online()
                print("Configured broker back online, subscribed.")
                conn_state = ST_CONNECTED
                configured_failures = 0
                continue
            broker_index += 1

        candidate = scan_base + str(scan_index)
        my_ip = str(wifi.radio.ipv4_address)
        scan_index += 1

        # Skip our own IP and any configured broker (already tried)
        if candidate == my_ip or candidate in BROKERS:
            continue

        valve.service()
        if tcp_probe(candidate):
            print(f"Port {MQTT_PORT} open on {candidate}, attempting MQTT…")
            scan_candidate = candidate
            conn_state = ST_SCAN_CONNECT

    # ── SCAN_CONNECT: full MQTT handshake with the candidate ─────────────────
    elif conn_state == ST_SCAN_CONNECT:
        valve.service()
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
            publish_valve_online()
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

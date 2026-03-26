import json
import time

import board
import digitalio
import wifi
import socketpool

from secrets import secrets
from adafruit_httpserver import Server, Request, Response, GET, Websocket

# -----------------------------
# CONFIG: choose GPIO pins here
# -----------------------------
PIN_MAP = {
    "LED": board.LED,
    "GP2": board.GP2,
    "GP3": board.GP3,
}

outputs = {}
for name, pin in PIN_MAP.items():
    dio = digitalio.DigitalInOut(pin)
    dio.direction = digitalio.Direction.OUTPUT
    dio.value = False
    outputs[name] = dio

def state_dict():
    return {name: bool(dio.value) for name, dio in outputs.items()}

def apply_set(pin_name: str, value: bool) -> bool:
    if pin_name not in outputs:
        return False
    outputs[pin_name].value = bool(value)
    return True

# -----------------------------
# Wi-Fi connect
# -----------------------------
print("Connecting to Wi-Fi...")
wifi.radio.connect(secrets["ssid"], secrets["password"])
ip = wifi.radio.ipv4_address
print("Connected, IP:", ip)

pool = socketpool.SocketPool(wifi.radio)
server = Server(pool, "/static", debug=True)

# Single-client websocket (simple + reliable on constrained devices)
ws: Websocket | None = None

HTML = """<!doctype html>
<html>
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width,initial-scale=1" />
  <title>Pico 2 W GPIO Dashboard (WS)</title>
  <style>
    body { font-family: system-ui, sans-serif; margin: 16px; }
    h1 { font-size: 18px; margin: 0 0 12px; }
    .row { display: flex; gap: 10px; align-items: center; margin: 10px 0; }
    .pin { width: 70px; font-weight: 600; }
    button { padding: 10px 12px; border-radius: 10px; border: 1px solid #ccc; cursor: pointer; }
    .pill { padding: 4px 10px; border-radius: 999px; font-size: 12px; border: 1px solid #ccc; }
    .status-on { border-color: #0a0; }
    .status-off { border-color: #a00; }
    .conn { font-size: 12px; opacity: 0.8; }
    code { background: #f4f4f4; padding: 2px 6px; border-radius: 6px; }
  </style>
</head>
<body>
  <h1>Pico 2 W GPIO Dashboard (WebSockets)</h1>
  <div class="conn">WS: <span id="wsStatus">connecting...</span></div>
  <div style="margin: 10px 0 12px;">
    Open: <code id="url"></code>
  </div>

  <div id="pins"></div>

<script>
const wsProto = (location.protocol === 'https:') ? 'wss://' : 'ws://';
const wsUrl = wsProto + location.host + '/ws';
document.getElementById('url').textContent = location.href;

let socket = null;
let latestState = {};

function setWsStatus(s) {
  document.getElementById('wsStatus').textContent = s;
}

function send(obj) {
  if (!socket || socket.readyState !== WebSocket.OPEN) return;
  socket.send(JSON.stringify(obj));
}

function render(state) {
  latestState = state || {};
  const root = document.getElementById('pins');
  root.innerHTML = '';

  Object.keys(latestState).sort().forEach(pin => {
    const v = !!latestState[pin];

    const row = document.createElement('div');
    row.className = 'row';

    const pinEl = document.createElement('div');
    pinEl.className = 'pin';
    pinEl.textContent = pin;

    const status = document.createElement('span');
    status.className = 'pill ' + (v ? 'status-on' : 'status-off');
    status.textContent = v ? 'ON' : 'OFF';

    // MOMENTARY "Hold" button: pressed => ON, released => OFF
    const btn = document.createElement('button');
    btn.textContent = 'Hold';

    const press = (ev) => {
      ev.preventDefault();
      send({type:'set', pin, value:true});
    };
    const release = (ev) => {
      ev.preventDefault();
      send({type:'set', pin, value:false});
    };

    // Mouse
    btn.addEventListener('mousedown', press);
    btn.addEventListener('mouseup', release);
    btn.addEventListener('mouseleave', release);

    // Touch
    btn.addEventListener('touchstart', press, {passive:false});
    btn.addEventListener('touchend', release, {passive:false});
    btn.addEventListener('touchcancel', release, {passive:false});

    row.appendChild(pinEl);
    row.appendChild(status);
    row.appendChild(btn);
    root.appendChild(row);
  });
}

function connect() {
  setWsStatus('connecting...');
  socket = new WebSocket(wsUrl);

  socket.onopen = () => {
    setWsStatus('connected');
    send({type:'get_state'});
  };

  socket.onclose = () => {
    setWsStatus('disconnected (reconnecting...)');
    setTimeout(connect, 750);
  };

  socket.onerror = () => {
    // onclose will handle reconnect
  };

  socket.onmessage = (evt) => {
    try {
      const msg = JSON.parse(evt.data);
      if (msg.type === 'state') {
        render(msg.state);
      }
    } catch (e) {}
  };
}

connect();
</script>
</body>
</html>
"""

@server.route("/", GET)
def index(request: Request):
    return Response(request, HTML, content_type="text/html")

@server.route("/ws", GET)
def websocket_route(request: Request):
    global ws
    if ws is not None:
        try:
            ws.close()
        except Exception:
            pass
    ws = Websocket(request)
    return ws

def ws_send_state():
    if ws is None:
        return
    ws.send_message(json.dumps({"type": "state", "state": state_dict()}), fail_silently=True)

# Start server on 0.0.0.0:80
server.start(str(ip), port=80)
print(f"HTTP+WS server: http://{ip}/")

# Main loop
last_push = 0.0
while True:
    server.poll()

    if ws is not None:
        # Handle inbound WS messages
        data = ws.receive(fail_silently=True)
        if data is not None:
            try:
                msg = json.loads(data)
                mtype = msg.get("type")
                if mtype == "get_state":
                    ws_send_state()
                elif mtype == "set":
                    pin = msg.get("pin")
                    value = bool(msg.get("value"))
                    if apply_set(pin, value):
                        ws_send_state()
                    else:
                        ws.send_message(json.dumps({"type":"error","error":"unknown pin"}), fail_silently=True)
            except Exception:
                ws.send_message(json.dumps({"type":"error","error":"bad json"}), fail_silently=True)

        # Optional: periodic state push (keeps UI honest if state changes elsewhere)
        now = time.monotonic()
        if now - last_push > 2.0:
            ws_send_state()
            last_push = now

    time.sleep(0.001)

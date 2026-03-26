import json
import time
import board
import digitalio
import wifi
import socketpool

from secrets import secrets

from adafruit_httpserver import Server, Request, Response, JSONResponse, POST

# -----------------------------
# CONFIG: choose GPIO pins here
# -----------------------------
# Pick pins that make sense for your wiring. Avoid using pins tied to special functions in your project.
PIN_MAP = {
    "LED": board.LED,
    "GP2": board.GP2,
    "GP3": board.GP3,
}

# Create DigitalInOut outputs
outputs = {}
for name, pin in PIN_MAP.items():
    dio = digitalio.DigitalInOut(pin)
    dio.direction = digitalio.Direction.OUTPUT
    dio.value = False
    outputs[name] = dio

def current_state():
    return {name: bool(dio.value) for name, dio in outputs.items()}

# -----------------------------
# Wi-Fi connect
# -----------------------------
print("Connecting to Wi-Fi...")
wifi.radio.connect(secrets["ssid"], secrets["password"])
ip = wifi.radio.ipv4_address
print("Connected, IP:", ip)

pool = socketpool.SocketPool(wifi.radio)
server = Server(pool, "/static", debug=True)

# -----------------------------
# HTML Dashboard (served inline)
# -----------------------------
def dashboard_html(ip):
    return f"""<!doctype html>
<html>
<head>
  <style>
    body {{ font-family: system-ui, sans-serif; margin: 16px; }}
    .row {{ display:flex; gap:10px; align-items:center; margin:10px 0; }}
    .pin {{ width:70px; font-weight:600; }}
    button {{ padding:10px 12px; border-radius:10px; border:1px solid #ccc; }}
  </style>
</head>
<body>
  <div>Device IP: <code>{ip}</code></div>
  <div id="pins"></div>

<script>
async function apiState() {{
  const r = await fetch('/api/state');
  return await r.json();
}}

async function apiSet(pin, value) {{
  await fetch('/api/set', {{
    method: 'POST',
    headers: {{ 'Content-Type':'application/json' }},
    body: JSON.stringify({{ pin: pin, value: value }})
  }});
}}

function render(state) {{
  const root = document.getElementById('pins');
  root.innerHTML = '';
  Object.keys(state).sort().forEach(pin => {{
    const row = document.createElement('div');
    row.className = 'row';

    const pinEl = document.createElement('div');
    pinEl.className = 'pin';
    pinEl.textContent = pin;

    const btn = document.createElement('button');
    btn.textContent = 'Hold';

    const press = async (e) => {{
      e.preventDefault();
      await apiSet(pin, true);
    }};
    const release = async (e) => {{
      e.preventDefault();
      await apiSet(pin, false);
    }};

    btn.addEventListener('mousedown', press);
    btn.addEventListener('mouseup', release);
    btn.addEventListener('mouseleave', release);
    btn.addEventListener('touchstart', press, {{ passive:false }});
    btn.addEventListener('touchend', release, {{ passive:false }});
    btn.addEventListener('touchcancel', release, {{ passive:false }});

    row.appendChild(pinEl);
    row.appendChild(btn);
    root.appendChild(row);
  }});
}}

async function refresh() {{
  render(await apiState());
}}
refresh();
</script>
</body>
</html>"""

# -----------------------------
# Routes
# -----------------------------
@server.route("/")
def index(request: Request):
    return Response(request, dashboard_html(wifi.radio.ipv4_address), content_type="text/html")

@server.route("/api/state")
def api_state(request: Request):
    return JSONResponse(request, current_state())

@server.route("/api/set", methods=[POST])
def api_set(request: Request):
    try:
        data = request.json()
        pin = data.get("pin")
        value = data.get("value")
        if pin not in outputs:
            return Response(request, "Unknown pin", status=400)
        outputs[pin].value = bool(value)
        return JSONResponse(request, {"ok": True, "state": current_state()})
    except Exception as e:
        return Response(request, "Bad request: " + repr(e), status=400)

# -----------------------------
# Start server
# -----------------------------
server.start(str(ip), port=80)
print("HTTP server started on http://%s/" % ip)

while True:
    server.poll()
    time.sleep(0.01)

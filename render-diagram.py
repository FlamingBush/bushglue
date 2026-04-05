#!/usr/bin/env python3
"""Render mqtt-architecture.svg directly — no external tools needed."""

import textwrap

W, H = 1100, 860

# ── Node definitions ────────────────────────────────────────────────
# Each node: id, x, y, width, height, lines[], shape, fill, stroke
NODES = [
    # external hardware
    dict(id="MIC",    x=100,  y=30,  w=140, h=44,
         lines=["Microphone"], shape="ellipse", fill="#d9ead3", stroke="#38761d"),
    dict(id="DISC_U", x=900,  y=30,  w=160, h=44,
         lines=["Discord User", "/pray command"], shape="ellipse", fill="#d9ead3", stroke="#38761d"),

    # services — row 1
    dict(id="STT",    x=30,   y=140, w=220, h=120,
         lines=["bush-stt", "stt-service.py", "──────────────",
                "Vosk STT", "mic input"],
         shape="box", fill="#cfe2f3", stroke="#1155cc"),
    dict(id="AGENT",  x=820,  y=140, w=220, h=100,
         lines=["audio-agent", "audio-agent.py", "──────────────",
                "PortAudio discovery"],
         shape="box", fill="#cfe2f3", stroke="#1155cc"),

    # services — row 2
    dict(id="T2V",    x=340,  y=340, w=260, h=120,
         lines=["bush-t2v", "t2v-service.py", "──────────────",
                "Rust HTTP :8765",
                "Ollama embed + ChromaDB :8000"],
         shape="box", fill="#cfe2f3", stroke="#1155cc"),

    # services — row 3
    dict(id="TTS",    x=30,   y=540, w=220, h=120,
         lines=["bush-tts", "tts-service.py", "──────────────",
                "espeak-ng", "sox reverb"],
         shape="box", fill="#cfe2f3", stroke="#1155cc"),
    dict(id="SENT",   x=380,  y=540, w=240, h=120,
         lines=["bush-sentiment", "sentiment-service.py", "──────────────",
                "DistilBERT emotion", "fire orchestration"],
         shape="box", fill="#cfe2f3", stroke="#1155cc"),
    dict(id="BOT",    x=730,  y=540, w=220, h=120,
         lines=["discord-bot", "discord-bot.py", "──────────────",
                "Discord VC bridge", "/pray injection"],
         shape="box", fill="#cfe2f3", stroke="#1155cc"),

    # speaker output
    dict(id="SPKR",   x=100,  y=745, w=160, h=44,
         lines=["Speaker / output"], shape="ellipse", fill="#d9ead3", stroke="#38761d"),
]

NODE = {n["id"]: n for n in NODES}

def cx(n): return n["x"] + n["w"] // 2
def cy(n): return n["y"] + n["h"] // 2
def right(n): return n["x"] + n["w"]
def bottom(n): return n["y"] + n["h"]

# ── Edges ────────────────────────────────────────────────────────────
# Each edge: src, dst, label, color, style, side hints
EDGES = [
    # audio I/O
    dict(src="MIC",    dst="STT",   label="audio",                  color="#38761d", dash=""),
    dict(src="TTS",    dst="SPKR",  label="espeak+sox",             color="#38761d", dash=""),

    # discord /pray  — route DISC_U→BOT down the right margin (clear of AGENT),
    #                   route BOT→STT over the top (clear of T2V)
    dict(src="DISC_U", dst="BOT",   label="/pray phrase",           color="#674ea7", dash="",
         path="M 1060 52 C 1095 52 1095 600 950 600",
         label_pos=(1055, 320)),
    dict(src="BOT",    dst="STT",   label="ALSA loopback",          color="#674ea7", dash="6,4",
         path="M 730 600 C 730 80 320 200 250 200",
         label_pos=(490, 160)),

    # main pipeline
    dict(src="STT",    dst="T2V",   label="stt/transcript\n{text, ts}", color="#1155cc", dash=""),
    dict(src="T2V",    dst="TTS",   label="t2v/verse\n{query,text,ts}", color="#1155cc", dash=""),
    dict(src="T2V",    dst="SENT",  label="t2v/verse",              color="#1155cc", dash=""),
    dict(src="T2V",    dst="BOT",   label="t2v/verse",              color="#1155cc", dash="4,3"),

    # TTS feedback to STT and SENT
    dict(src="TTS",    dst="STT",   label="tts/speaking\ntts/done", color="#cc4125", dash="5,3"),
    dict(src="TTS",    dst="SENT",  label="tts/done",               color="#cc4125", dash="5,3"),

    # sentiment to discord
    dict(src="SENT",   dst="BOT",   label="sentiment/result",       color="#999999", dash="4,3"),

    # audio device management
    dict(src="AGENT",  dst="STT",   label="audio/devices\n(retained)", color="#999999", dash="3,3"),
    # route AGENT→TTS down the left margin (clear of T2V/SENT)
    dict(src="AGENT",  dst="TTS",   label="audio/devices\n(retained)", color="#999999", dash="3,3",
         path="M 820 190 C 500 190 20 300 20 400 C 20 500 430 600 250 600",
         label_pos=(75, 390)),
]

# ── SVG helpers ──────────────────────────────────────────────────────

def svg_node(n):
    x, y, w, h = n["x"], n["y"], n["w"], n["h"]
    fill, stroke = n["fill"], n["stroke"]
    parts = []
    if n["shape"] == "ellipse":
        rx, ry = w // 2, h // 2
        parts.append(
            f'<ellipse cx="{x+rx}" cy="{y+ry}" rx="{rx}" ry="{ry}" '
            f'fill="{fill}" stroke="{stroke}" stroke-width="1.5"/>'
        )
    else:
        parts.append(
            f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="6" ry="6" '
            f'fill="{fill}" stroke="{stroke}" stroke-width="1.5"/>'
        )
    # text lines
    line_h = 15
    total = len(n["lines"]) * line_h
    start_y = y + (h - total) // 2 + 11
    for i, line in enumerate(n["lines"]):
        if line.startswith("──"):
            parts.append(
                f'<text x="{x + w//2}" y="{start_y + i*line_h}" '
                f'text-anchor="middle" font-family="monospace" font-size="10">{line}</text>'
            )
        else:
            bold = i == 0
            weight = ' font-weight="bold"' if bold else ''
            parts.append(
                f'<text x="{x + w//2}" y="{start_y + i*line_h}" '
                f'text-anchor="middle" font-family="Helvetica,Arial,sans-serif" '
                f'font-size="11"{weight}>{line}</text>'
            )
    return "\n".join(parts)


def midpoint(x1, y1, x2, y2):
    return (x1 + x2) / 2, (y1 + y2) / 2


def arrow_path(src_n, dst_n, color, dash, offset_x=0, offset_y=0):
    x1, y1 = cx(src_n), bottom(src_n)
    x2, y2 = cx(dst_n), dst_n["y"]

    # Special overrides for sideways/angled connections
    # TTS → STT: right side of STT to left of TTS (both same row, STT is to left)
    # Actually TTS is below STT in a different row

    dash_attr = f' stroke-dasharray="{dash}"' if dash else ""
    path = f"M {x1+offset_x} {y1} C {x1+offset_x} {y1+40} {x2+offset_x} {y2-40} {x2+offset_x} {y2}"
    mid_x = (x1 + x2) / 2 + offset_x
    mid_y = (y1 + y2) / 2
    return path, mid_x, mid_y, dash_attr


def build_edge(e):
    src_n = NODE[e["src"]]
    dst_n = NODE[e["dst"]]
    color = e["color"]
    dash = e["dash"]
    label = e["label"]

    if "path" in e:
        # Custom routed path — avoids routing through other nodes
        path = e["path"]
        lx, ly = e.get("label_pos", (0, 0))
    else:
        sx, sy = cx(src_n), cy(src_n)
        dx, dy = cx(dst_n), cy(dst_n)

        # Determine attachment points based on relative position
        if abs(sx - dx) < 60:
            # Mostly vertical
            if sy < dy:
                # downward: exit bottom of src, enter top of dst
                x1, y1 = cx(src_n), bottom(src_n)
                x2, y2 = cx(dst_n), dst_n["y"]
                ctrl_dy = abs(y2 - y1) * 0.4
                path = f"M {x1} {y1} C {x1} {y1+ctrl_dy} {x2} {y2-ctrl_dy} {x2} {y2}"
            else:
                # upward: exit top of src, enter bottom of dst
                x1, y1 = cx(src_n), src_n["y"]
                x2, y2 = cx(dst_n), bottom(dst_n)
                ctrl_dy = abs(y2 - y1) * 0.4
                # control points pull upward so path arrives at dst going upward
                path = f"M {x1} {y1} C {x1} {y1-ctrl_dy} {x2} {y2+ctrl_dy} {x2} {y2}"
            lx = (x1 + x2) / 2 + 8
            ly = (y1 + y2) / 2
        else:
            # More horizontal — use side attachment
            if sx < dx:
                # left-to-right: exit right side of src, enter left side of dst
                x1, y1 = right(src_n), cy(src_n)
                x2, y2 = dst_n["x"], cy(dst_n)
                ctrl_dx = abs(x2 - x1) * 0.45
                path = f"M {x1} {y1} C {x1+ctrl_dx} {y1} {x2-ctrl_dx} {y2} {x2} {y2}"
            else:
                # right-to-left: exit left side of src, enter right side of dst
                x1, y1 = src_n["x"], cy(src_n)
                x2, y2 = right(dst_n), cy(dst_n)
                ctrl_dx = abs(x2 - x1) * 0.45
                # control points pull leftward so path arrives at dst going left
                path = f"M {x1} {y1} C {x1-ctrl_dx} {y1} {x2+ctrl_dx} {y2} {x2} {y2}"
            lx = (x1 + x2) / 2
            ly = (y1 + y2) / 2 - 8

    dash_attr = f' stroke-dasharray="{dash}"' if dash else ""
    parts = []
    parts.append(
        f'<path d="{path}" fill="none" stroke="{color}" stroke-width="1.5"'
        f'{dash_attr} marker-end="url(#arrow-{color.replace("#","")})"/>'
    )
    if label:
        lines = label.split("\n")
        for i, ln in enumerate(lines):
            parts.append(
                f'<text x="{lx:.0f}" y="{ly + i*13:.0f}" text-anchor="middle" '
                f'font-family="Helvetica,Arial,sans-serif" font-size="9" fill="{color}" '
                f'paint-order="stroke" stroke="white" stroke-width="3" stroke-linejoin="round"'
                f'>{ln}</text>'
            )
    return "\n".join(parts)


def arrowhead_def(color):
    cid = color.replace("#", "")
    return (
        f'<marker id="arrow-{cid}" markerWidth="8" markerHeight="6" '
        f'refX="8" refY="3" orient="auto">'
        f'<polygon points="0 0, 8 3, 0 6" fill="{color}"/>'
        f'</marker>'
    )


# ── Assemble SVG ─────────────────────────────────────────────────────

colors = list({e["color"] for e in EDGES})

defs = "\n".join(arrowhead_def(c) for c in colors)

nodes_svg = "\n\n".join(svg_node(n) for n in NODES)
edges_svg = "\n\n".join(build_edge(e) for e in EDGES)

svg = f"""<?xml version="1.0" encoding="UTF-8"?>
<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" viewBox="0 0 {W} {H}">
  <defs>
    {defs}
  </defs>
  <rect width="{W}" height="{H}" fill="white"/>

  <!-- legend -->
  <text x="10" y="{H-60}" font-family="Helvetica,Arial,sans-serif" font-size="10" fill="#333">Legend:</text>
  <line x1="70" y1="{H-63}" x2="100" y2="{H-63}" stroke="#1155cc" stroke-width="1.5" marker-end="url(#arrow-1155cc)"/>
  <text x="105" y="{H-60}" font-family="Helvetica,Arial,sans-serif" font-size="10" fill="#333">MQTT pipeline</text>
  <line x1="200" y1="{H-63}" x2="230" y2="{H-63}" stroke="#cc4125" stroke-width="1.5" stroke-dasharray="5,3" marker-end="url(#arrow-cc4125)"/>
  <text x="235" y="{H-60}" font-family="Helvetica,Arial,sans-serif" font-size="10" fill="#333">feedback / mute</text>
  <line x1="350" y1="{H-63}" x2="380" y2="{H-63}" stroke="#38761d" stroke-width="1.5" marker-end="url(#arrow-38761d)"/>
  <text x="385" y="{H-60}" font-family="Helvetica,Arial,sans-serif" font-size="10" fill="#333">audio I/O</text>
  <line x1="560" y1="{H-63}" x2="590" y2="{H-63}" stroke="#674ea7" stroke-width="1.5" stroke-dasharray="6,4" marker-end="url(#arrow-674ea7)"/>
  <text x="595" y="{H-60}" font-family="Helvetica,Arial,sans-serif" font-size="10" fill="#333">Discord /pray</text>
  <line x1="675" y1="{H-63}" x2="705" y2="{H-63}" stroke="#999999" stroke-width="1.5" stroke-dasharray="3,3" marker-end="url(#arrow-999999)"/>
  <text x="710" y="{H-60}" font-family="Helvetica,Arial,sans-serif" font-size="10" fill="#333">device config</text>

  <!-- edges (drawn first so nodes appear on top) -->
  {edges_svg}

  <!-- nodes -->
  {nodes_svg}

</svg>"""

out = "/home/user/bushglue/mqtt-architecture.svg"
with open(out, "w") as f:
    f.write(svg)
print(f"Written: {out}")

try:
    import cairosvg
    png_out = "/home/user/bushglue/mqtt-architecture.png"
    cairosvg.svg2png(url=out, write_to=png_out, scale=2)
    print(f"Written: {png_out}")
except ImportError:
    print("cairosvg not installed — skipping PNG export (pip install cairosvg)")

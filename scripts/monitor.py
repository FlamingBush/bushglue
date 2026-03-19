#!/usr/bin/env python3
"""
Real-time pipeline monitor for Bush Glue.
Subscribes to all MQTT topics and renders them as a live TUI.

Usage: python3 monitor.py
"""
import json
import subprocess
import sys
import threading
import time
from collections import deque
from datetime import datetime

import paho.mqtt.client as mqtt
from rich import box
from rich.columns import Columns
from rich.console import Console
from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

# ── MQTT ───────────────────────────────────────────────────────────────────
MQTT_PORT = 1883
TOPICS = [
    "bush/pipeline/stt/transcript",
    "bush/pipeline/t2v/verse",
    "bush/pipeline/sentiment/result",
    "bush/flame/flare/pulse",
    "bush/flame/bigjet/pulse",
    "bush/pipeline/tts/speaking",
]

# ── fire hardware limits (ms valve on-time) ────────────────────────────────
FLARE_MAX = 3000
BIGJET_MAX = 1000

# ── emotion colours ────────────────────────────────────────────────────────
EMOTION_COLOUR = {
    "anger":    "red",
    "fear":     "dark_orange",
    "surprise": "yellow",
    "joy":      "green",
    "love":     "magenta",
    "sadness":  "blue",
}

LOG_MAX = 12
BAR_WIDTH = 28

# ── bush ASCII art ─────────────────────────────────────────────────────────
# 11 chars wide, 7 rows.  Characters:
#   ,  →  stems/twigs      *  →  foliage
#   ~  →  small flame      )( →  flame curl
#   ^  →  jet tip          \/ →  jet spread
#   |  →  trunk or jet column

_BUSH_ART = {
    # calm green bush, dim pilot light
    "cold": [
        "   ,*,   ",
        "  (*,*)  ",
        " (*,*,*) ",
        "  (*,*)  ",
        "   ,*,   ",
        "   |||   ",
        "   |||   ",
    ],
    # flare valve open — fire through foliage, no jet
    "flare": [
        "  )~*~(  ",
        " )~*~*~( ",
        "  ~*~*~  ",
        " )~*~*~( ",
        "  )~*~(  ",
        "   |||   ",
        "   |||   ",
    ],
    # bigjet valve open — vertical jet through cold (unlit) bush
    "bigjet_cold": [
        "    ^    ",
        "   \\|/   ",
        "  (,|,)  ",
        " (,*|*,) ",
        "  (,|,)  ",
        "   |||   ",
        "   |||   ",
    ],
    # both valves open — jet through burning bush
    "bigjet_flare": [
        "    ^    ",
        "   \\|/   ",
        "  )~|~(  ",
        " )~*|*~( ",
        "  )~|~(  ",
        "   |||   ",
        "   |||   ",
    ],
}


def _color_bush_line(line: str, row: int, bush_state: str) -> Text:
    """Return a richly-coloured Text for one row of bush art."""
    on_fire  = bush_state in ("flare", "bigjet_flare")
    has_jet  = bush_state in ("bigjet_cold", "bigjet_flare")
    t = Text()
    for ch in line:
        if ch == " ":
            t.append(" ")
        elif ch == "^":
            t.append(ch, style="bold bright_white")
        elif ch in r"\/":
            t.append(ch, style="bold red1")
        elif ch == "~":
            t.append(ch, style="orange3" if has_jet else "yellow")
        elif ch == "*":
            if on_fire:
                t.append(ch, style="bold bright_yellow" if has_jet else "bold yellow")
            else:
                t.append(ch, style="green")
        elif ch in "()":
            t.append(ch, style="bold orange3" if on_fire else "dark_green")
        elif ch == ",":
            t.append(ch, style="dark_green")
        elif ch == "|":
            if row >= 5:    # trunk rows
                t.append(ch, style="bold orange3" if on_fire else "dim yellow")
            else:           # jet column
                t.append(ch, style="bold bright_white")
        else:
            t.append(ch)
    return t


def _flare_active(s: "State") -> bool:
    return bool(s.flare_ts and s.flare_ms > 0
                and (time.time() - s.flare_ts) * 1000 < s.flare_ms)


def _bigjet_active(s: "State") -> bool:
    return bool(s.bigjet_ts and s.bigjet_ms > 0
                and (time.time() - s.bigjet_ts) * 1000 < s.bigjet_ms)


def _windows_host_ip() -> str:
    try:
        with open("/proc/version") as f:
            if "microsoft" not in f.read().lower():
                return "localhost"
    except OSError:
        return "localhost"
    result = subprocess.run(["ip", "route", "show"], capture_output=True, text=True)
    for line in result.stdout.splitlines():
        if line.startswith("default"):
            return line.split()[2]
    return "localhost"


# ── shared state (written by MQTT thread, read by render thread) ───────────
class State:
    def __init__(self):
        self.lock = threading.Lock()
        self.connected = False
        self.broker = "?"
        self.stt_text = ""
        self.stt_ts: float | None = None
        self.verse_query = ""
        self.verse_text = ""
        self.verse_ts: float | None = None
        self.scores: list[dict] = []          # [{label, score}, ...]
        self.sentiment_ts: float | None = None
        self.flare_ms = 0
        self.flare_ts: float | None = None
        self.bigjet_ms = 0
        self.bigjet_ts: float | None = None
        self.tts_text = ""
        self.tts_ts: float | None = None
        self.log: deque[tuple[float, str, str]] = deque(maxlen=LOG_MAX)
        # (ts, tag, message)


state = State()


# ── bar helpers ────────────────────────────────────────────────────────────
def _bar(value: int, maximum: int, width: int, colour: str) -> Text:
    """Render a filled progress bar as a rich Text object."""
    filled = int(width * min(value, maximum) / maximum) if maximum else 0
    empty = width - filled
    t = Text()
    t.append("█" * filled, style=f"bold {colour}")
    t.append("░" * empty, style="dim")
    return t


def _age_style(ts: float | None) -> str:
    """Dim text if the last update was more than 5 s ago."""
    if ts is None:
        return "dim"
    age = time.time() - ts
    if age < 2:
        return "bold"
    if age < 5:
        return ""
    return "dim"


def _fmt_ts(ts: float | None) -> str:
    if ts is None:
        return "--:--:--"
    return datetime.fromtimestamp(ts).strftime("%H:%M:%S")


# ── panel builders ─────────────────────────────────────────────────────────
def build_stt_panel(s: State) -> Panel:
    style = _age_style(s.stt_ts)
    text = Text()
    if s.stt_text:
        text.append(f'"{s.stt_text}"', style=f"{style} italic cyan")
        text.append(f"   {_fmt_ts(s.stt_ts)}", style="dim")
    else:
        text.append("waiting for speech…", style="dim")
    return Panel(text, title="[bold]STT[/bold]  [dim]bush/pipeline/stt/transcript[/dim]",
                 box=box.ROUNDED, border_style="cyan")


def build_verse_panel(s: State) -> Panel:
    text = Text()
    if s.verse_text:
        style = _age_style(s.verse_ts)
        text.append(f'Query:  ', style="dim")
        text.append(f'"{s.verse_query}"\n', style=f"{style} italic")
        text.append(f'Verse:  ', style="dim")
        text.append(f'"{s.verse_text}"', style=f"{style} italic yellow")
        text.append(f"   {_fmt_ts(s.verse_ts)}", style="dim")
    else:
        text.append("waiting for transcript…", style="dim")
    return Panel(text, title="[bold]TEXT-TO-VERSE[/bold]  [dim]bush/pipeline/t2v/verse[/dim]",
                 box=box.ROUNDED, border_style="yellow")


def build_sentiment_panel(s: State) -> Panel:
    text = Text()
    if s.scores:
        ts_style = _age_style(s.sentiment_ts)
        all6 = sorted(s.scores, key=lambda x: x["score"], reverse=True)
        for row_start in range(0, len(all6), 3):
            row = all6[row_start:row_start + 3]
            for col, item in enumerate(row):
                idx    = row_start + col
                label  = item["label"]
                score  = item["score"]
                colour = EMOTION_COLOUR.get(label, "white")
                pct    = int(score * 100)
                bold   = "bold " if idx == 0 else ""
                prefix = "► " if idx == 0 else "  "
                if col > 0:
                    text.append("   ")
                text.append(f"{prefix}{label:<9}", style=f"{bold}{colour}")
                text.append_text(_bar(pct, 100, 10, colour))
                text.append(f" {pct:3d}%", style=f"{bold}{ts_style}")
            text.append("\n")
    else:
        text.append("waiting for verse…", style="dim")
    return Panel(text, title="[bold]SENTIMENT[/bold]  [dim]bush/pipeline/sentiment/result[/dim]",
                 box=box.ROUNDED, border_style="magenta")


def build_bush_panel(s: State) -> Panel:
    flare  = _flare_active(s)
    bigjet = _bigjet_active(s)

    if bigjet and flare:
        bush_state   = "bigjet_flare"
        label_markup = "[bold bright_white]BIG JET[/bold bright_white] [bold orange3]+ FLARE[/bold orange3]"
        border       = "bright_white"
    elif bigjet:
        bush_state   = "bigjet_cold"
        label_markup = "[bold bright_white]BIG JET[/bold bright_white]"
        border       = "bright_white"
    elif flare:
        bush_state   = "flare"
        label_markup = "[bold orange3]FLARE[/bold orange3]"
        border       = "orange3"
    else:
        bush_state   = "cold"
        label_markup = "[dim green]standby[/dim green]"
        border       = "dark_green"

    text = Text(justify="center")
    for row, line in enumerate(_BUSH_ART[bush_state]):
        text.append_text(_color_bush_line(line, row, bush_state))
        text.append("\n")
    text.append("\n")
    text.append_text(Text.from_markup(label_markup))

    return Panel(text, title="[bold]Bush[/bold]",
                 box=box.ROUNDED, border_style=border)


def build_fire_panel(s: State) -> Panel:
    text = Text()

    # Flare
    flare_style = _age_style(s.flare_ts)
    text.append("Flare   ", style="bold red")
    text.append(_bar(s.flare_ms, FLARE_MAX, BAR_WIDTH, "red"))
    text.append(f"  {s.flare_ms:>5} ms", style=f"bold red {flare_style}")
    text.append(f"   max {FLARE_MAX} ms\n", style="dim")

    # Big Jet
    bigjet_style = _age_style(s.bigjet_ts)
    text.append("Big Jet ", style="bold dark_orange")
    text.append(_bar(s.bigjet_ms, BIGJET_MAX, BAR_WIDTH, "dark_orange"))
    text.append(f"  {s.bigjet_ms:>5} ms", style=f"bold dark_orange {bigjet_style}")
    text.append(f"   max {BIGJET_MAX} ms", style="dim")

    return Panel(text, title="[bold]FIRE CONTROL[/bold]  [dim]bush/flame/*/pulse  (valve on-time ms)[/dim]",
                 box=box.ROUNDED, border_style="red")


def build_tts_panel(s: State) -> Panel:
    text = Text()
    if s.tts_text:
        style = _age_style(s.tts_ts)
        age = time.time() - s.tts_ts if s.tts_ts else 99
        indicator = "[bold green]▶ SPEAKING[/bold green]" if age < 8 else "[dim]last:[/dim]"
        text.append_text(Text.from_markup(indicator))
        text.append(f"  {s.tts_text[:120]}", style=f"{style} italic")
        text.append(f"   {_fmt_ts(s.tts_ts)}", style="dim")
    else:
        text.append("waiting for verse…", style="dim")
    return Panel(text, title="[bold]TTS[/bold]  [dim]espeak-ng[/dim]",
                 box=box.ROUNDED, border_style="green")


def build_log_panel(s: State) -> Panel:
    text = Text()
    entries = list(s.log)
    for ts, tag, msg in reversed(entries):
        tag_colour = {
            "TRANSCRIPT": "cyan",
            "VERSE":      "yellow",
            "SENTIMENT":  "magenta",
            "FLARE":      "red",
            "BIGJET":     "dark_orange",
            "TTS":        "green",
        }.get(tag, "white")
        text.append(f"{_fmt_ts(ts)}  ", style="dim")
        text.append(f"{tag:<12}", style=f"bold {tag_colour}")
        text.append(f"{msg}\n", style="")
    return Panel(text, title="[bold]Log[/bold]", box=box.ROUNDED, border_style="dim")


def build_header(s: State) -> Text:
    t = Text()
    status = "[bold green]● CONNECTED[/bold green]" if s.connected else "[bold red]● DISCONNECTED[/bold red]"
    t.append("Bush Glue Pipeline Monitor", style="bold white")
    t.append("   ")
    t.append_text(Text.from_markup(status))
    t.append(f"   broker {s.broker}:{MQTT_PORT}", style="dim")
    t.append(f"   {datetime.now().strftime('%H:%M:%S')}", style="dim")
    return t


def render(s: State) -> Layout:
    layout = Layout()
    layout.split_column(
        Layout(name="header",    size=1),
        Layout(name="top",       size=5),
        Layout(name="sentiment", size=4),
        Layout(name="bottom",    size=11),
        Layout(name="log"),
    )
    layout["top"].split_row(
        Layout(build_stt_panel(s),   name="stt"),
        Layout(build_verse_panel(s), name="verse"),
    )
    layout["sentiment"].update(build_sentiment_panel(s))
    layout["bottom"].split_row(
        Layout(build_bush_panel(s), name="bush", ratio=2),
        Layout(build_tts_panel(s),  name="tts",  ratio=3),
    )
    layout["log"].update(build_log_panel(s))
    layout["header"].update(build_header(s))
    return layout


# ── MQTT callbacks ─────────────────────────────────────────────────────────
def on_connect(client, userdata, flags, reason_code, properties):
    with state.lock:
        state.connected = (str(reason_code) == "Success")
    for topic in TOPICS:
        client.subscribe(topic)


def on_disconnect(client, userdata, flags, reason_code, properties):
    with state.lock:
        state.connected = False


def on_message(client, userdata, msg):
    topic = msg.topic
    now = time.time()
    try:
        with state.lock:
            if topic == "bush/pipeline/stt/transcript":
                data = json.loads(msg.payload)
                state.stt_text = data.get("text", "")
                state.stt_ts = now
                state.log.append((now, "TRANSCRIPT", f'"{state.stt_text[:80]}"'))

            elif topic == "bush/pipeline/t2v/verse":
                data = json.loads(msg.payload)
                state.verse_query = data.get("query", "")
                state.verse_text = data.get("text", "").replace("\n", " ")
                state.verse_ts = now
                state.log.append((now, "VERSE", f'"{state.verse_text[:80]}"'))

            elif topic == "bush/pipeline/sentiment/result":
                data = json.loads(msg.payload)
                state.scores = data.get("classification", [])
                state.sentiment_ts = now
                top = sorted(state.scores, key=lambda x: x["score"], reverse=True)
                if top:
                    label = top[0]["label"]
                    score = top[0]["score"]
                    flare = data.get("flare", 0)
                    bigjet = data.get("bigjet", 0)
                    state.log.append((now, "SENTIMENT",
                                      f"{label} {score:.2f}  flare={flare}ms  bigjet={bigjet}ms"))

            elif topic == "bush/flame/flare/pulse":
                ms = int(msg.payload.decode())
                state.flare_ms = ms
                state.flare_ts = now
                state.log.append((now, "FLARE", f"{ms} ms"))

            elif topic == "bush/flame/bigjet/pulse":
                ms = int(msg.payload.decode())
                state.bigjet_ms = ms
                state.bigjet_ts = now
                state.log.append((now, "BIGJET", f"{ms} ms"))

            elif topic == "bush/pipeline/tts/speaking":
                data = json.loads(msg.payload)
                state.tts_text = data.get("text", "")
                state.tts_ts = now
                state.log.append((now, "TTS", f'"{state.tts_text[:80]}"'))

    except Exception as e:
        with state.lock:
            state.log.append((now, "ERROR", str(e)))


# ── main ───────────────────────────────────────────────────────────────────
def main():
    broker = _windows_host_ip()
    with state.lock:
        state.broker = broker

    mqttc = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
    mqttc.on_connect = on_connect
    mqttc.on_disconnect = on_disconnect
    mqttc.on_message = on_message

    try:
        mqttc.connect(broker, MQTT_PORT, 60)
    except Exception as e:
        print(f"Cannot connect to MQTT broker at {broker}:{MQTT_PORT}: {e}", file=sys.stderr)
        sys.exit(1)

    mqttc.loop_start()

    console = Console()
    try:
        with Live(render(state), console=console, refresh_per_second=8,
                  screen=True, vertical_overflow="visible") as live:
            while True:
                time.sleep(0.125)
                with state.lock:
                    live.update(render(state))
    except KeyboardInterrupt:
        pass
    finally:
        mqttc.loop_stop()
        mqttc.disconnect()


if __name__ == "__main__":
    main()

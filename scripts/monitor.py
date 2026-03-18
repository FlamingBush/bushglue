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


def _windows_host_ip() -> str:
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
        style = _age_style(s.sentiment_ts)
        top3 = sorted(s.scores, key=lambda x: x["score"], reverse=True)[:3]
        for i, item in enumerate(top3):
            label = item["label"]
            score = item["score"]
            colour = EMOTION_COLOUR.get(label, "white")
            pct = int(score * 100)
            bar = _bar(pct, 100, 20, colour)
            prefix = "► " if i == 0 else "  "
            bold = "bold " if i == 0 else ""
            text.append(f"{prefix}{label:<10}", style=f"{bold}{colour}")
            text.append(bar)
            text.append(f"  {pct:3d}%", style=f"{bold}{style}")
            text.append("\n")
        text.append(f"{_fmt_ts(s.sentiment_ts)}", style="dim")
    else:
        text.append("waiting for verse…", style="dim")
    return Panel(text, title="[bold]SENTIMENT[/bold]  [dim]bush/pipeline/sentiment/result[/dim]",
                 box=box.ROUNDED, border_style="magenta")


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
        Layout(name="header", size=1),
        Layout(name="top", size=5),
        Layout(name="middle", size=8),
        Layout(name="fire", size=5),
        Layout(name="log"),
    )
    layout["top"].split_row(
        Layout(build_stt_panel(s), name="stt"),
        Layout(build_verse_panel(s), name="verse"),
    )
    layout["middle"].update(build_sentiment_panel(s))
    layout["fire"].update(build_fire_panel(s))
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
        with Live(render(state), console=console, refresh_per_second=4,
                  screen=True, vertical_overflow="visible") as live:
            while True:
                time.sleep(0.25)
                with state.lock:
                    live.update(render(state))
    except KeyboardInterrupt:
        pass
    finally:
        mqttc.loop_stop()
        mqttc.disconnect()


if __name__ == "__main__":
    main()

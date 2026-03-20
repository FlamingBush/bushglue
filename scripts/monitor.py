#!/usr/bin/env python3
"""
Real-time pipeline monitor for Bush Glue.
Subscribes to all MQTT topics and renders them as a live TUI.

Usage: python3 monitor.py

Keys:
  i        — select STT input device
  o        — select TTS output device
  j/↓      — move cursor down
  k/↑      — move cursor up
  Enter    — confirm selection
  Esc      — cancel
  0-9      — jump to device index
  q        — quit
"""
import json
import sys
import termios
import threading
import time
import tty
from collections import deque
from datetime import datetime

import paho.mqtt.client as mqtt
from rich import box
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
    "bush/pipeline/tts/done",
    "bush/audio/devices",
    "bush/audio/stt/device",
    "bush/audio/tts/device",
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
_BUSH_ART = {
    "cold": [
        "   ,*,   ",
        "  (*,*)  ",
        " (*,*,*) ",
        "  (*,*)  ",
        "   ,*,   ",
        "   |||   ",
        "   |||   ",
    ],
    "flare": [
        "  )~*~(  ",
        " )~*~*~( ",
        "  ~*~*~  ",
        " )~*~*~( ",
        "  )~*~(  ",
        "   |||   ",
        "   |||   ",
    ],
    "bigjet_cold": [
        "    ^    ",
        "   \\|/   ",
        "  (,|,)  ",
        " (,*|*,) ",
        "  (,|,)  ",
        "   |||   ",
        "   |||   ",
    ],
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
            if row >= 5:
                t.append(ch, style="bold orange3" if on_fire else "dim yellow")
            else:
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


from bushutil import get_mqtt_broker


# ── shared state ───────────────────────────────────────────────────────────
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
        self.scores: list[dict] = []
        self.sentiment_ts: float | None = None
        self.flare_ms = 0
        self.flare_ts: float | None = None
        self.bigjet_ms = 0
        self.bigjet_ts: float | None = None
        self.t2v_processing = False
        self.tts_text = ""
        self.tts_ts: float | None = None
        self.tts_speaking = False
        self.log: deque[tuple[float, str, str]] = deque(maxlen=LOG_MAX)
        # audio device selection
        self.capture_devices: list[dict] = []
        self.playback_devices: list[dict] = []
        self.current_input: dict | None = None
        self.current_output: dict | None = None
        self.ui_mode = "normal"        # "normal" | "select_input" | "select_output" | "input_text"
        self.selected_index = 0
        self.input_text = ""
        self.quit = False


state = State()
_mqttc: mqtt.Client | None = None


# ── bar helpers ────────────────────────────────────────────────────────────
def _bar(value: int, maximum: int, width: int, colour: str) -> Text:
    filled = int(width * min(value, maximum) / maximum) if maximum else 0
    empty = width - filled
    t = Text()
    t.append("█" * filled, style=f"bold {colour}")
    t.append("░" * empty, style="dim")
    return t


def _age_style(ts: float | None) -> str:
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
    border = "bold bright_yellow" if s.t2v_processing else "yellow"
    return Panel(text, title="[bold]TEXT-TO-VERSE[/bold]  [dim]bush/pipeline/t2v/verse[/dim]",
                 box=box.ROUNDED, border_style=border)


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

    flare_style = _age_style(s.flare_ts)
    text.append("Flare   ", style="bold red")
    text.append(_bar(s.flare_ms, FLARE_MAX, BAR_WIDTH, "red"))
    text.append(f"  {s.flare_ms:>5} ms", style=f"bold red {flare_style}")
    text.append(f"   max {FLARE_MAX} ms\n", style="dim")

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
        style = "bold" if s.tts_speaking else _age_style(s.tts_ts)
        indicator = "[bold green]▶ SPEAKING[/bold green]" if s.tts_speaking else "[dim]last:[/dim]"
        text.append_text(Text.from_markup(indicator))
        text.append(f"  {s.tts_text}", style=f"{style} italic")
        text.append(f"   {_fmt_ts(s.tts_ts)}", style="dim")
    else:
        text.append("waiting for verse…", style="dim")
    border = "bold bright_green" if s.tts_speaking else "green"
    return Panel(text, title="[bold]TTS[/bold]  [dim]espeak-ng[/dim]",
                 box=box.ROUNDED, border_style=border)


def build_log_panel(s: State) -> Panel:
    # Audio device info + key hints embedded in the title
    in_dev  = _fmt_device(s.current_input)
    out_dev = _fmt_device(s.current_output)
    title = (f"[bold]Log[/bold]"
             f"  [dim]In:[/dim] [cyan]{in_dev}[/cyan]"
             f"  [dim]Out:[/dim] [green]{out_dev}[/green]"
             f"  [bold cyan]\\[i][/bold cyan][dim]nput[/dim]"
             f"  [bold green]\\[o][/bold green][dim]utput[/dim]"
             f"  [bold yellow]\\[t][/bold yellow][dim]ranscript[/dim]"
             f"  [bold white]\\[q][/bold white][dim]uit[/dim]")
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
            "TTS DONE":   "dim green",
        }.get(tag, "white")
        text.append(f"{_fmt_ts(ts)}  ", style="dim")
        text.append(f"{tag:<12}", style=f"bold {tag_colour}")
        text.append(f"{msg}\n", style="")
    return Panel(text, title=title, box=box.ROUNDED, border_style="dim")


def build_input_panel(s: State) -> Panel:
    text = Text()
    text.append(s.input_text, style="bold white")
    text.append("█", style="blink white")   # block cursor
    return Panel(text,
                 title="[bold yellow]Inject Transcript[/bold yellow]"
                       "  [dim]Enter=send  Esc=cancel[/dim]",
                 box=box.ROUNDED, border_style="yellow")


def _fmt_device(dev: dict | None) -> str:
    if dev is None:
        return "unknown"
    d = dev.get("device")
    if d is None:
        return "default"
    return str(d)


def build_audio_panel(s: State) -> Panel:
    """Device selector panel, used only in select_input / select_output modes."""
    if s.ui_mode == "select_input":
        devices = s.capture_devices
        title   = "[bold cyan]Select STT Input Device[/bold cyan]  [dim]Enter=confirm  Esc=cancel[/dim]"
        border  = "cyan"
    else:
        devices = s.playback_devices
        title   = "[bold green]Select TTS Output Device[/bold green]  [dim]Enter=confirm  Esc=cancel[/dim]"
        border  = "green"

    text = Text()
    if not devices:
        text.append("No devices found — is audio-agent running?", style="dim")
    else:
        for i, dev in enumerate(devices):
            cursor = "► " if i == s.selected_index else "  "
            style  = "bold reverse" if i == s.selected_index else ""
            text.append(cursor, style="bold yellow" if i == s.selected_index else "dim")
            text.append(f"[{dev['index']:2d}]  {dev['name']}", style=style)
            text.append(f"  {int(dev['sr'])} Hz  {dev['channels']}ch\n",
                        style="dim" if i != s.selected_index else "dim reverse")
    return Panel(text, title=title, box=box.ROUNDED, border_style=border)


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
    if s.ui_mode in ("select_input", "select_output"):
        layout["log"].update(build_audio_panel(s))
    elif s.ui_mode == "input_text":
        layout["log"].update(build_input_panel(s))
    else:
        layout["log"].update(build_log_panel(s))

    layout["top"].split_row(
        Layout(build_stt_panel(s),   name="stt"),
        Layout(build_verse_panel(s), name="verse"),
    )
    layout["sentiment"].update(build_sentiment_panel(s))
    layout["bottom"].split_row(
        Layout(build_bush_panel(s), name="bush", ratio=2),
        Layout(build_tts_panel(s),  name="tts",  ratio=3),
    )
    layout["header"].update(build_header(s))
    return layout


# ── keyboard input ─────────────────────────────────────────────────────────
import os
import select as _select


def _read_key(fd: int) -> str:
    """Read one keypress from an already-raw fd. Returns a string token."""
    ch = os.read(fd, 1).decode("utf-8", errors="ignore")
    if ch == "\x1b":
        r, _, _ = _select.select([fd], [], [], 0.05)
        if r:
            ch2 = os.read(fd, 1).decode("utf-8", errors="ignore")
            if ch2 == "[":
                r2, _, _ = _select.select([fd], [], [], 0.05)
                if r2:
                    ch3 = os.read(fd, 1).decode("utf-8", errors="ignore")
                    if ch3 == "A":
                        return "UP"
                    elif ch3 == "B":
                        return "DOWN"
                    return f"ESC[{ch3}"
            return f"ESC{ch2}"
        return "ESC"
    return ch


def _handle_key(key: str):
    global _mqttc
    with state.lock:
        mode = state.ui_mode

    if mode == "normal":
        if key == "i":
            with state.lock:
                state.ui_mode = "select_input"
                state.selected_index = 0
            if _mqttc:
                _mqttc.publish("bush/audio/discover", "{}")
        elif key == "o":
            with state.lock:
                state.ui_mode = "select_output"
                state.selected_index = 0
            if _mqttc:
                _mqttc.publish("bush/audio/discover", "{}")
        elif key == "t":
            with state.lock:
                state.ui_mode = "input_text"
                state.input_text = ""
        elif key in ("q", "Q", "\x03"):   # q or Ctrl-C
            with state.lock:
                state.quit = True

    elif mode == "input_text":
        if key == "ESC":
            with state.lock:
                state.ui_mode = "normal"
        elif key in ("\r", "\n"):   # Enter — send
            with state.lock:
                text = state.input_text.strip()
                state.ui_mode = "normal"
                state.input_text = ""
            if text and _mqttc:
                _mqttc.publish("bush/pipeline/stt/transcript",
                               json.dumps({"text": text, "ts": time.time()}))
        elif key in ("\x7f", "\x08"):   # Backspace / Delete
            with state.lock:
                state.input_text = state.input_text[:-1]
        elif len(key) == 1 and key.isprintable():
            with state.lock:
                state.input_text += key

    elif mode in ("select_input", "select_output"):
        with state.lock:
            if mode == "select_input":
                devices = state.capture_devices
            else:
                devices = state.playback_devices
            n = len(devices)

        if key in ("j", "DOWN") and n > 0:
            with state.lock:
                state.selected_index = (state.selected_index + 1) % n
        elif key in ("k", "UP") and n > 0:
            with state.lock:
                state.selected_index = (state.selected_index - 1) % n
        elif key.isdigit():
            with state.lock:
                idx = int(key)
                if 0 <= idx < n:
                    state.selected_index = idx
        elif key in ("\r", "\n"):    # Enter — confirm
            with state.lock:
                sel = state.selected_index
                if mode == "select_input":
                    devices = state.capture_devices
                    topic   = "bush/audio/stt/set-device"
                else:
                    devices = state.playback_devices
                    topic   = "bush/audio/tts/set-device"
                state.ui_mode = "normal"
            if _mqttc and devices and sel < len(devices):
                dev = devices[sel]
                if mode == "select_input":
                    payload = json.dumps({"device": dev["index"]})
                else:
                    payload = json.dumps({"device": dev["name"]})
                _mqttc.publish(topic, payload)
        elif key == "ESC":
            with state.lock:
                state.ui_mode = "normal"


def _keyboard_thread():
    """Daemon thread reading keypresses from stdin. Sets raw mode once for its lifetime."""
    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    try:
        tty.setcbreak(fd)
        while True:
            key = _read_key(fd)
            _handle_key(key)
            with state.lock:
                if state.quit:
                    break
    except Exception:
        pass
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)


# ── MQTT callbacks ─────────────────────────────────────────────────────────
def on_connect(client, userdata, flags, reason_code, properties):
    with state.lock:
        state.connected = (str(reason_code) == "Success")
    for topic in TOPICS:
        client.subscribe(topic)
    # Request fresh device list
    client.publish("bush/audio/discover", "{}")


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
                state.t2v_processing = True
                state.log.append((now, "TRANSCRIPT", f'"{state.stt_text[:80]}"'))

            elif topic == "bush/pipeline/t2v/verse":
                data = json.loads(msg.payload)
                state.verse_query = data.get("query", "")
                state.verse_text = data.get("text", "").replace("\n", " ")
                state.verse_ts = now
                state.t2v_processing = False
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
                state.tts_speaking = True
                state.log.append((now, "TTS", f'"{state.tts_text[:80]}"'))

            elif topic == "bush/pipeline/tts/done":
                state.tts_speaking = False
                state.log.append((now, "TTS DONE", ""))

            elif topic == "bush/audio/devices":
                data = json.loads(msg.payload)
                state.capture_devices  = data.get("capture", [])
                state.playback_devices = data.get("playback", [])

            elif topic == "bush/audio/stt/device":
                state.current_input = json.loads(msg.payload)

            elif topic == "bush/audio/tts/device":
                state.current_output = json.loads(msg.payload)

    except Exception as e:
        with state.lock:
            state.log.append((now, "ERROR", str(e)))


# ── main ───────────────────────────────────────────────────────────────────
def main():
    global _mqttc
    broker = get_mqtt_broker()
    with state.lock:
        state.broker = broker

    mqttc = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
    mqttc.on_connect = on_connect
    mqttc.on_disconnect = on_disconnect
    mqttc.on_message = on_message
    _mqttc = mqttc

    try:
        mqttc.connect(broker, MQTT_PORT, 60)
    except Exception as e:
        print(f"Cannot connect to MQTT broker at {broker}:{MQTT_PORT}: {e}", file=sys.stderr)
        sys.exit(1)

    mqttc.loop_start()

    # Start keyboard input thread (only if stdin is a tty)
    if sys.stdin.isatty():
        kb = threading.Thread(target=_keyboard_thread, daemon=True)
        kb.start()

    console = Console()
    try:
        with Live(render(state), console=console, refresh_per_second=8,
                  screen=True, vertical_overflow="visible") as live:
            while True:
                time.sleep(0.125)
                with state.lock:
                    should_quit = state.quit
                if should_quit:
                    break
                with state.lock:
                    live.update(render(state))
    except KeyboardInterrupt:
        pass
    finally:
        mqttc.loop_stop()
        mqttc.disconnect()


if __name__ == "__main__":
    main()

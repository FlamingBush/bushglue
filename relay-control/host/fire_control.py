#!/usr/bin/env python3
"""
fire_control.py — MQTT flame controller TUI
Requires: paho-mqtt  (pip install paho-mqtt)

QWERTY layout philosophy:
  LEFT half  → BIGJET  (big, sustained, powerful)
  RIGHT half → FLARE   (sharp, short, precise)

Key rows map to pulse durations — top row = shortest, bottom row = longest.
Within each half the keys are ordered left→right = shorter→longer.
"""

import curses
import time
import threading
import collections
import paho.mqtt.client as mqtt

BROKER       = "localhost"
PORT         = 1883
TOPIC_FLARE  = "bush/flame/flare/pulse"
TOPIC_BIGJET = "bush/flame/bigjet/pulse"

# ── Key → (topic, duration_ms, label) ───────────────────────────────────────
# Left half of QWERTY  = BIGJET
# Right half of QWERTY = FLARE
#
# Row layout (each row sorted short→long, left→right on keyboard):
#
#  NUMBER ROW  (not used — reserved for future macros)
#  TOP ROW     1 = shortest pulses
#  HOME ROW    2 = medium pulses
#  BOTTOM ROW  3 = longest pulses

BIGJET_COLOR = 1   # cyan
FLARE_COLOR  = 2   # orange/red

KEY_MAP = {
    # ── TOP ROW — short pulses ───────────────────────────────────────────────
    ord('q'): (TOPIC_BIGJET,  50,   "Q",  "50ms",  BIGJET_COLOR),
    ord('w'): (TOPIC_BIGJET,  100,  "W",  "100ms", BIGJET_COLOR),
    ord('e'): (TOPIC_BIGJET,  150,  "E",  "150ms", BIGJET_COLOR),
    ord('r'): (TOPIC_BIGJET,  200,  "R",  "200ms", BIGJET_COLOR),
    ord('t'): (TOPIC_BIGJET,  300,  "T",  "300ms", BIGJET_COLOR),
    ord('y'): (TOPIC_FLARE,   50,   "Y",  "50ms",  FLARE_COLOR),
    ord('u'): (TOPIC_FLARE,   100,  "U",  "100ms", FLARE_COLOR),
    ord('i'): (TOPIC_FLARE,   150,  "I",  "150ms", FLARE_COLOR),
    ord('o'): (TOPIC_FLARE,   200,  "O",  "200ms", FLARE_COLOR),
    ord('p'): (TOPIC_FLARE,   300,  "P",  "300ms", FLARE_COLOR),

    # ── HOME ROW — medium pulses ─────────────────────────────────────────────
    ord('a'): (TOPIC_BIGJET,  400,  "A",  "400ms", BIGJET_COLOR),
    ord('s'): (TOPIC_BIGJET,  600,  "S",  "600ms", BIGJET_COLOR),
    ord('d'): (TOPIC_BIGJET,  800,  "D",  "800ms", BIGJET_COLOR),
    ord('f'): (TOPIC_BIGJET,  1000, "F",  "1.0s",  BIGJET_COLOR),
    ord('g'): (TOPIC_BIGJET,  1500, "G",  "1.5s",  BIGJET_COLOR),
    ord('h'): (TOPIC_FLARE,   400,  "H",  "400ms", FLARE_COLOR),
    ord('j'): (TOPIC_FLARE,   600,  "J",  "600ms", FLARE_COLOR),
    ord('k'): (TOPIC_FLARE,   800,  "K",  "800ms", FLARE_COLOR),
    ord('l'): (TOPIC_FLARE,   1000, "L",  "1.0s",  FLARE_COLOR),
    ord(';'): (TOPIC_FLARE,   1500, ";",  "1.5s",  FLARE_COLOR),

    # ── BOTTOM ROW — long pulses ─────────────────────────────────────────────
    ord('z'): (TOPIC_BIGJET,  2000, "Z",  "2.0s",  BIGJET_COLOR),
    ord('x'): (TOPIC_BIGJET,  3000, "X",  "3.0s",  BIGJET_COLOR),
    ord('c'): (TOPIC_BIGJET,  4000, "C",  "4.0s",  BIGJET_COLOR),
    ord('v'): (TOPIC_BIGJET,  5000, "V",  "5.0s",  BIGJET_COLOR),
    ord('b'): (TOPIC_BIGJET,  8000, "B",  "8.0s",  BIGJET_COLOR),
    ord('n'): (TOPIC_FLARE,   2000, "N",  "2.0s",  FLARE_COLOR),
    ord('m'): (TOPIC_FLARE,   3000, "M",  "3.0s",  FLARE_COLOR),
    ord(','): (TOPIC_FLARE,   4000, ",",  "4.0s",  FLARE_COLOR),
    ord('.'): (TOPIC_FLARE,   5000, ".",  "5.0s",  FLARE_COLOR),
    ord('/'): (TOPIC_FLARE,   8000, "/",  "8.0s",  FLARE_COLOR),
}

# ── Keyboard visual layout ───────────────────────────────────────────────────
ROWS = [
    list("qwertyuiop"),
    list("asdfghjkl;"),
    list("zxcvbnm,./"),
]

ROW_LABELS = ["SHORT", "MEDIUM", "LONG"]

# ── Event log ────────────────────────────────────────────────────────────────
MAX_LOG = 40
log_entries = collections.deque(maxlen=MAX_LOG)
log_lock    = threading.Lock()

# ── Active pulse state (for live bar display) ────────────────────────────────
active = {
    TOPIC_FLARE:  {"until": 0.0, "duration": 0, "key": ""},
    TOPIC_BIGJET: {"until": 0.0, "duration": 0, "key": ""},
}
active_lock = threading.Lock()

# ── MQTT ─────────────────────────────────────────────────────────────────────
mqtt_status = {"connected": False, "msg": "Connecting…"}

def on_connect(client, userdata, flags, rc, props=None):
    if rc == 0:
        mqtt_status["connected"] = True
        mqtt_status["msg"] = f"Connected  {BROKER}:{PORT}"
    else:
        mqtt_status["connected"] = False
        mqtt_status["msg"] = f"MQTT error rc={rc}"

def on_disconnect(client, userdata, rc, props=None, *args):
    mqtt_status["connected"] = False
    mqtt_status["msg"] = "Disconnected — reconnecting…"

mq = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
mq.on_connect    = on_connect
mq.on_disconnect = on_disconnect

def mqtt_thread():
    try:
        mq.connect(BROKER, PORT, keepalive=10)
    except Exception as e:
        mqtt_status["msg"] = f"Connect failed: {e}"
    mq.loop_forever()

threading.Thread(target=mqtt_thread, daemon=True).start()

# ── Fire! ─────────────────────────────────────────────────────────────────────
def fire(topic, duration_ms, key_label):
    if not mqtt_status["connected"]:
        with log_lock:
            log_entries.appendleft(
                (time.time(), key_label, topic, duration_ms, False)
            )
        return
    mq.publish(topic, str(duration_ms), qos=0)
    now = time.time()
    with active_lock:
        active[topic]["until"]    = now + duration_ms / 1000.0
        active[topic]["duration"] = duration_ms
        active[topic]["key"]      = key_label
    with log_lock:
        log_entries.appendleft((now, key_label, topic, duration_ms, True))

# ── Drawing helpers ───────────────────────────────────────────────────────────
def safe_addstr(win, y, x, text, attr=0):
    h, w = win.getmaxyx()
    if y < 0 or y >= h or x < 0:
        return
    if x + len(text) > w:
        text = text[:w - x]
    try:
        win.addstr(y, x, text, attr)
    except curses.error:
        pass

def draw_bar(win, y, x, width, fraction, color_pair):
    filled = max(0, min(width, int(width * fraction)))
    bar_on  = "█" * filled
    bar_off = "░" * (width - filled)
    safe_addstr(win, y, x, bar_on,  curses.color_pair(color_pair) | curses.A_BOLD)
    safe_addstr(win, y, x + filled, bar_off, curses.color_pair(5))

# ── Main TUI ──────────────────────────────────────────────────────────────────
def main(stdscr):
    curses.curs_set(0)
    stdscr.nodelay(True)
    stdscr.timeout(33)   # ~30 fps

    curses.start_color()
    curses.use_default_colors()
    curses.init_pair(1, curses.COLOR_CYAN,    -1)   # BIGJET
    curses.init_pair(2, curses.COLOR_RED,     -1)   # FLARE
    curses.init_pair(3, 8,                    -1)   # dim (dark grey)
    curses.init_pair(4, curses.COLOR_WHITE,   -1)   # bright white
    curses.init_pair(5, curses.COLOR_BLACK,   curses.COLOR_BLACK)  # bar bg
    curses.init_pair(6, curses.COLOR_YELLOW,  -1)   # accent
    curses.init_pair(7, curses.COLOR_GREEN,   -1)   # ok
    curses.init_pair(8, curses.COLOR_RED,     -1)   # error

    C_BIGJET  = curses.color_pair(1) | curses.A_BOLD
    C_FLARE   = curses.color_pair(2) | curses.A_BOLD
    C_DIM     = curses.color_pair(3)
    C_WHITE   = curses.color_pair(4) | curses.A_BOLD
    C_ACCENT  = curses.color_pair(6) | curses.A_BOLD
    C_OK      = curses.color_pair(7)
    C_ERR     = curses.color_pair(8) | curses.A_BOLD

    while True:
        h, w = stdscr.getmaxyx()
        stdscr.erase()

        now = time.time()

        # ── Title bar ────────────────────────────────────────────────────────
        title = " 🔥 FIRE CONTROL  "
        safe_addstr(stdscr, 0, 0, "─" * w, C_DIM)
        safe_addstr(stdscr, 0, max(0, (w - len(title)) // 2), title, C_WHITE)
        conn_text = f" ● {mqtt_status['msg']} "
        conn_color = C_OK if mqtt_status["connected"] else C_ERR
        safe_addstr(stdscr, 0, w - len(conn_text) - 1, conn_text, conn_color)

        # ── Section headers ───────────────────────────────────────────────────
        mid = w // 2
        safe_addstr(stdscr, 2, 2,       "〓  B I G J E T  〓", C_BIGJET)
        safe_addstr(stdscr, 2, mid + 2, "〓  F L A R E  〓",   C_FLARE)
        safe_addstr(stdscr, 3, 0, "─" * w, C_DIM)

        # ── Keyboard grid ─────────────────────────────────────────────────────
        CELL = 9
        ROW_START_Y = 4
        INDENT = [1, 2, 3]

        for row_i, row_keys in enumerate(ROWS):
            y = ROW_START_Y + row_i * 3
            indent = INDENT[row_i]

            label = ROW_LABELS[row_i]
            safe_addstr(stdscr, y,     0, f"{label:>6}", C_DIM)
            safe_addstr(stdscr, y + 1, 0, " " * 6,      C_DIM)

            for col_i, ch in enumerate(row_keys):
                x = 7 + indent + col_i * CELL
                info = KEY_MAP.get(ord(ch))
                if info is None:
                    continue
                topic, dur, key_lbl, dur_lbl, side = info
                color = C_BIGJET if side == BIGJET_COLOR else C_FLARE

                with active_lock:
                    a = active[topic]
                    remaining = a["until"] - now
                    is_active = remaining > 0 and a["key"] == key_lbl

                box_attr = color | curses.A_REVERSE if is_active else color

                top_line    = f"┌─{key_lbl:^3}─┐"
                mid_line    = f"│{dur_lbl:^7}│"
                bottom_line = f"└───────┘"

                safe_addstr(stdscr, y,     x, top_line,    box_attr)
                safe_addstr(stdscr, y + 1, x, mid_line,    box_attr)
                safe_addstr(stdscr, y + 2, x, bottom_line, box_attr)

            safe_addstr(stdscr, y,     mid, "│", C_DIM)
            safe_addstr(stdscr, y + 1, mid, "│", C_DIM)
            safe_addstr(stdscr, y + 2, mid, "│", C_DIM)

        # ── Active pulse bars ─────────────────────────────────────────────────
        bar_y = ROW_START_Y + len(ROWS) * 3 + 1
        safe_addstr(stdscr, bar_y, 0, "─" * w, C_DIM)
        bar_y += 1

        bar_w = mid - 6
        for label, topic, color, cx in [
            ("BIGJET", TOPIC_BIGJET, 1, 1),
            ("FLARE",  TOPIC_FLARE,  2, mid + 1),
        ]:
            with active_lock:
                a = active[topic]
                remaining  = max(0.0, a["until"] - now)
                dur        = a["duration"]
                frac       = (remaining / (dur / 1000.0)) if dur > 0 else 0.0

            color_attr = curses.color_pair(color) | curses.A_BOLD
            safe_addstr(stdscr, bar_y,     cx, f"{label}", color_attr)
            safe_addstr(stdscr, bar_y + 1, cx, f"{'FIRING' if frac > 0 else 'IDLE':^6}", color_attr)
            bx = cx + 7
            bw = bar_w - 8
            draw_bar(stdscr, bar_y + 1, bx, bw, frac, color)
            if frac > 0:
                ms_left = int(remaining * 1000)
                safe_addstr(stdscr, bar_y + 1, bx + bw + 1,
                             f"{ms_left:>5}ms", color_attr)

        # ── Event log ─────────────────────────────────────────────────────────
        log_y = bar_y + 3
        safe_addstr(stdscr, log_y, 0, "─" * w, C_DIM)
        log_y += 1
        safe_addstr(stdscr, log_y, 1, "EVENT LOG", C_ACCENT)
        log_y += 1

        with log_lock:
            entries = list(log_entries)

        max_log_lines = h - log_y - 2
        for i, (ts, key, topic, dur, ok) in enumerate(entries[:max_log_lines]):
            t_str    = time.strftime("%H:%M:%S", time.localtime(ts))
            is_flare = topic == TOPIC_FLARE
            name     = "FLARE " if is_flare else "BIGJET"
            color    = C_FLARE if is_flare else C_BIGJET
            ok_str   = "✓" if ok else "✗"
            ok_color = C_OK if ok else C_ERR
            line     = f"  {t_str}  [{key}]  "
            safe_addstr(stdscr, log_y + i, 0, line, C_DIM)
            xo = len(line)
            safe_addstr(stdscr, log_y + i, xo, ok_str + " ", ok_color)
            safe_addstr(stdscr, log_y + i, xo + 2, f"{name} {dur}ms", color)

        # ── Footer ────────────────────────────────────────────────────────────
        footer = " Q/W/E/R/T  A/S/D/F/G  Z/X/C/V/B → BIGJET   Y/U/I/O/P  H/J/K/L/;  N/M/,./  → FLARE    ESC quit "
        safe_addstr(stdscr, h - 1, 0, "─" * w, C_DIM)
        safe_addstr(stdscr, h - 1, max(0, (w - len(footer)) // 2), footer, C_DIM)

        stdscr.refresh()

        # ── Input ─────────────────────────────────────────────────────────────
        ch = stdscr.getch()
        if ch == 27:   # ESC
            break
        if ch in KEY_MAP:
            topic, dur, key_lbl, dur_lbl, _ = KEY_MAP[ch]
            fire(topic, dur, key_lbl)

    mq.disconnect()

if __name__ == "__main__":
    curses.wrapper(main)

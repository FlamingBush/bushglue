#!/usr/bin/env python3
"""
Push-to-talk button watcher.

Reads a GPIO line and publishes press/release on bush/pipeline/stt/ptt.

Also drives the button's indicator LED from bush/pipeline/stt/listening, so
the light means "you are being heard" rather than "the switch is closed".
Those differ: while the bush is speaking, bush-stt is muted and a press does
nothing, and an LED wired across the switch contacts would light anyway.

Runs as its own service rather than inside bush-stt for two reasons: the
button can then live on any host that can reach the broker, and bush-stt
stays testable without hardware — publishing the same two MQTT messages by
hand drives the identical code path.

Default wiring is the Orange Pi 5 Ultra 40-pin header:

    header pin 18  =  GPIO1_A4  =  kernel gpio 36  =  /dev/gpiochip1 line 4
    header pin 20  =  GND         (button)
    header pin 16  =  GPIO1_A3  =  kernel gpio 35  =  /dev/gpiochip1 line 3
    header pin 14  =  GND         (LED, through a series resistor)

The LED pin sources at most a few mA. Anything brighter than a bare indicator
LED — or an LED whose internal resistor is sized for 5 V or 12 V — needs a
transistor and its own rail; set PTT_LED_ACTIVE_LOW=1 for a low-side driver
that sinks rather than sources.

Button shorts the line to ground, internal pull-up holds it high when open,
so the line is active-low. /dev/gpiochip* is root-only by default; see
udev/95-gpio.rules for the group grant.

The rk3588 pinctrl driver silently ignores the gpiochip character device's
BIAS flag: requesting bias="pull_up" is accepted and does nothing, so the
line floats and reads as a permanently-held button. The pad pull-up has to be
set through the SoC's own registers instead, which is what PTT_PULLUP_CMD
does before the line is opened. Pad config is lost on power-down, so this has
to run on every start, not once at install.
"""
import json
import os
import subprocess
import threading
import time

from bushutil import make_logger, run_mqtt_service

# ── GPIO config ────────────────────────────────────────────────────────────
PTT_GPIO_CHIP = os.environ.get("PTT_GPIO_CHIP", "/dev/gpiochip1")
PTT_GPIO_LINE = int(os.environ.get("PTT_GPIO_LINE", "4"))
PTT_ACTIVE_LOW = os.environ.get("PTT_ACTIVE_LOW", "1") not in ("0", "false", "False", "")
PTT_BIAS = os.environ.get("PTT_BIAS", "pull_up")

# Applied before opening the line; see the note above on the ignored BIAS
# flag. wiringOP's -1 means physical header numbering, so 18 is the pin you
# wired. Set empty to skip (e.g. if you fitted an external pull-up resistor,
# which is the more robust option and makes this unnecessary).
PTT_PULLUP_CMD = os.environ.get("PTT_PULLUP_CMD", "gpio -1 mode 18 up")

# Indicator LED. Set PTT_LED_LINE to an empty string to run without one.
PTT_LED_CHIP = os.environ.get("PTT_LED_CHIP", "/dev/gpiochip1")
PTT_LED_LINE = os.environ.get("PTT_LED_LINE", "3")
PTT_LED_ACTIVE_LOW = os.environ.get("PTT_LED_ACTIVE_LOW", "0") not in ("0", "false", "False", "")

# Contact bounce on a cheap momentary switch settles well inside this.
PTT_DEBOUNCE_MS = int(os.environ.get("PTT_DEBOUNCE_MS", "25"))

# ── MQTT ───────────────────────────────────────────────────────────────────
TOPIC_PTT = "bush/pipeline/stt/ptt"
TOPIC_LISTENING = "bush/pipeline/stt/listening"

log = make_logger("ptt")


def _apply_pad_pullup() -> None:
    """Set the pad pull-up via the SoC registers, since BIAS is a no-op here."""
    cmd = PTT_PULLUP_CMD.strip()
    if not cmd:
        return
    try:
        r = subprocess.run(cmd.split(), capture_output=True, text=True, timeout=10)
    except Exception as e:
        log(f"WARNING: pull-up command {cmd!r} failed to run: {e}")
        return
    if r.returncode != 0:
        log(f"WARNING: pull-up command {cmd!r} exited {r.returncode}: "
            f"{(r.stderr or '').strip()}")
        log("Without a pull-up the button line floats and reads permanently DOWN.")
    else:
        log(f"pad pull-up applied ({cmd})")


def _open_line():
    """Open the button line for both-edge events. Raises on failure."""
    from periphery import GPIO

    _apply_pad_pullup()
    return GPIO(
        PTT_GPIO_CHIP,
        PTT_GPIO_LINE,
        "in",
        edge="both",
        bias=PTT_BIAS,
        label="bush-ptt",
    )


def _is_pressed(level: bool) -> bool:
    """Map a raw line level to button state."""
    return (not level) if PTT_ACTIVE_LOW else level


# ── indicator LED ──────────────────────────────────────────────────────────
# Written from the MQTT callback thread; the button line is read from the
# watch thread. Separate GPIO objects, but the handle is shared, so guard it.
_led = [None]
_led_lock = threading.Lock()


def _open_led():
    """Open the LED line, or return None if not configured / unavailable."""
    if not PTT_LED_LINE.strip():
        log("no LED configured (PTT_LED_LINE empty)")
        return None
    from periphery import GPIO

    try:
        led = GPIO(PTT_LED_CHIP, int(PTT_LED_LINE), "out", label="bush-ptt-led")
    except Exception as e:
        # A missing LED must never take the button down with it.
        log(f"WARNING: cannot open LED on {PTT_LED_CHIP} line {PTT_LED_LINE}: {e}")
        return None
    log(f"LED on {PTT_LED_CHIP} line {PTT_LED_LINE} "
        f"(active_low={PTT_LED_ACTIVE_LOW})")
    return led


def _set_led(on: bool) -> None:
    with _led_lock:
        led = _led[0]
        if led is None:
            return
        try:
            led.write((not on) if PTT_LED_ACTIVE_LOW else on)
        except Exception as e:
            log(f"LED write error: {e}")


def on_message(client, userdata, msg):
    """Drive the LED from bush-stt's listening state."""
    if msg.topic != TOPIC_LISTENING:
        return
    try:
        listening = bool(json.loads(msg.payload).get("listening"))
    except Exception as e:
        log(f"listening parse error: {e}")
        return
    _set_led(listening)


def watch(client, stop):
    """Background loop: translate GPIO edges into MQTT press/release."""
    try:
        line = _open_line()
    except Exception as e:
        log(f"ERROR: cannot open {PTT_GPIO_CHIP} line {PTT_GPIO_LINE}: {e}")
        log("Is the user in the 'gpio' group? See udev/95-gpio.rules.")
        return

    pressed = _is_pressed(line.read())
    log(f"watching {PTT_GPIO_CHIP} line {PTT_GPIO_LINE} "
        f"(active_low={PTT_ACTIVE_LOW}, bias={PTT_BIAS}) — "
        f"button is {'DOWN' if pressed else 'up'} at startup")
    _publish(client, pressed)

    # Timestamp of the last accepted edge, for debounce.
    last_change = 0.0
    press_started = time.time() if pressed else None

    try:
        while not stop.is_set():
            # Short poll timeout so stop.set() is noticed promptly.
            if not line.poll(timeout=0.5):
                continue
            line.read_event()

            now = time.time()
            if (now - last_change) * 1000 < PTT_DEBOUNCE_MS:
                continue

            # Read the settled level rather than trusting the edge direction;
            # a burst of bounce can leave the two out of step.
            level_pressed = _is_pressed(line.read())
            if level_pressed == pressed:
                continue

            pressed = level_pressed
            last_change = now

            if pressed:
                press_started = now
                log("button PRESSED")
            else:
                held_ms = int((now - press_started) * 1000) if press_started else 0
                log(f"button RELEASED (held {held_ms} ms)")
            _publish(client, pressed)
    finally:
        line.close()
        log("released GPIO line")


def _publish(client, pressed: bool) -> None:
    client.publish(TOPIC_PTT, json.dumps({"pressed": pressed, "ts": time.time()}))


def _shutdown():
    """Dark LED on the way out — a latched-on light reads as 'still listening'."""
    _set_led(False)
    with _led_lock:
        if _led[0] is not None:
            try:
                _led[0].close()
            except Exception:
                pass
            _led[0] = None


def main():
    with _led_lock:
        _led[0] = _open_led()
    _set_led(False)
    run_mqtt_service("ptt", [TOPIC_LISTENING], on_message,
                     background_loop=watch, on_shutdown=_shutdown)


if __name__ == "__main__":
    main()

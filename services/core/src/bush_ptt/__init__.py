#!/usr/bin/env python3
"""
Push-to-talk button watcher.

Reads a GPIO line and publishes press/release on bush/pipeline/stt/ptt.

Runs as its own service rather than inside bush-stt for two reasons: the
button can then live on any host that can reach the broker, and bush-stt
stays testable without hardware — publishing the same two MQTT messages by
hand drives the identical code path.

Default wiring is the Orange Pi 5 Ultra 40-pin header:

    header pin 18  =  GPIO1_A4  =  kernel gpio 36  =  /dev/gpiochip1 line 4
    header pin 20  =  GND

Button shorts the line to ground, internal pull-up holds it high when open,
so the line is active-low. /dev/gpiochip* is root-only by default; see
udev/95-gpio.rules for the group grant.
"""
import json
import os
import time

from bushutil import make_logger, run_mqtt_service

# ── GPIO config ────────────────────────────────────────────────────────────
PTT_GPIO_CHIP = os.environ.get("PTT_GPIO_CHIP", "/dev/gpiochip1")
PTT_GPIO_LINE = int(os.environ.get("PTT_GPIO_LINE", "4"))
PTT_ACTIVE_LOW = os.environ.get("PTT_ACTIVE_LOW", "1") not in ("0", "false", "False", "")
PTT_BIAS = os.environ.get("PTT_BIAS", "pull_up")

# Contact bounce on a cheap momentary switch settles well inside this.
PTT_DEBOUNCE_MS = int(os.environ.get("PTT_DEBOUNCE_MS", "25"))

# ── MQTT ───────────────────────────────────────────────────────────────────
TOPIC_PTT = "bush/pipeline/stt/ptt"

log = make_logger("ptt")


def _open_line():
    """Open the button line for both-edge events. Raises on failure."""
    from periphery import GPIO

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


def main():
    run_mqtt_service("ptt", [], lambda *a: None, background_loop=watch)


if __name__ == "__main__":
    main()

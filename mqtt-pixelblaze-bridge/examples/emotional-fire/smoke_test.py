#!/usr/bin/env python3
"""Publish a visible Emotional Fire sequence to a local MQTT broker."""

from __future__ import annotations

import argparse
import json
import time
from typing import Any


LAYOUT_TOPIC = "bush/lights/layout"
BRIGHTNESS_TOPIC = "bush/lights/brightness"
TIMEOUT_TOPIC = "bush/lights/speaking-timeout-seconds"
ANTICIPATION_TIMEOUT_TOPIC = "bush/lights/anticipation-timeout-seconds"
DECAY_TOPIC = "bush/lights/decay-seconds"
SPIRAL_DIRECTION_TOPIC = "bush/lights/spiral-center-at-zero"
SENTIMENT_TOPIC = "bush/pipeline/sentiment/result"
VERSE_TOPIC = "bush/pipeline/t2v/verse"
SPEAKING_TOPIC = "bush/pipeline/tts/speaking"
DONE_TOPIC = "bush/pipeline/tts/done"
FLAME_TOPIC = "bush/flame/pulse"
TEST_PAUSE_SECONDS = 30
DEFAULT_COMPARISON_GAP_SECONDS = 2


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Drive the Emotional Fire Pixelblaze scenes through MQTT.",
    )
    parser.add_argument("--host", default="127.0.0.1", help="MQTT broker host")
    parser.add_argument("--port", type=int, default=1883, help="MQTT broker port")
    parser.add_argument("--username")
    parser.add_argument("--password")
    parser.add_argument(
        "--layout",
        choices=("spiral", "unordered"),
        default="spiral",
    )
    parser.add_argument(
        "--scenario",
        choices=(
            "normal",
            "emotions",
            "watchdog",
            "races",
            "flame",
            "both",
            "all",
        ),
        default="normal",
    )
    parser.add_argument(
        "--hold-seconds",
        type=float,
        default=6,
        help="How long normal and emotion-comparison cases remain speaking",
    )
    parser.add_argument(
        "--watchdog-seconds",
        type=float,
        default=8,
        help="Speaking timeout used by the missing-done scenario (minimum 5)",
    )
    parser.add_argument(
        "--anticipation-timeout-seconds",
        type=float,
        default=30,
    )
    parser.add_argument("--decay-seconds", type=float, default=35)
    parser.add_argument(
        "--spiral-center",
        choices=("first", "last"),
        default="first",
        help="Whether fixture index 0 or the last index is the spiral center",
    )
    parser.add_argument(
        "--settle-seconds",
        type=float,
        default=2,
        help="Pause after selecting the Pixelblaze pattern",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print messages without connecting to MQTT",
    )
    parser.add_argument(
        "--allow-flame-pulses",
        action="store_true",
        help="Required for any live run that publishes imperative valve commands",
    )
    parser.add_argument(
        "--with-flame-pulses",
        action="store_true",
        help="Overlay flame commands on each emotion-comparison case",
    )
    parser.add_argument(
        "--pause-between-tests",
        action="store_true",
        help="Pause 30 seconds between each selected test case",
    )
    args = parser.parse_args()
    if args.password and not args.username:
        parser.error("--password requires --username")
    if args.hold_seconds < 0 or args.settle_seconds < 0:
        parser.error("wait durations cannot be negative")
    if args.watchdog_seconds < 5:
        parser.error("--watchdog-seconds must be at least 5")
    if args.anticipation_timeout_seconds < 5:
        parser.error("--anticipation-timeout-seconds must be at least 5")
    if args.decay_seconds < 5:
        parser.error("--decay-seconds must be at least 5")
    if args.with_flame_pulses and args.scenario not in ("emotions", "all"):
        parser.error("--with-flame-pulses requires --scenario emotions or all")
    flame_requested = args.scenario in ("flame", "all") or args.with_flame_pulses
    if flame_requested and not args.dry_run and not args.allow_flame_pulses:
        parser.error(
            "flame pulse scenarios can actuate a connected relay; use an "
            "isolated broker and pass --allow-flame-pulses"
        )
    return args


def connect(args: argparse.Namespace) -> Any:
    if args.dry_run:
        return None

    import paho.mqtt.client as mqtt

    client = mqtt.Client(
        mqtt.CallbackAPIVersion.VERSION2,
        client_id="emotional-fire-smoke",
    )
    if args.username:
        client.username_pw_set(args.username, args.password)
    client.connect(args.host, args.port, 60)
    client.loop_start()
    return client


def publish(client: Any, topic: str, payload: str, *, qos: int = 1) -> None:
    print(f"{topic} <- {payload}", flush=True)
    if client is None:
        return
    result = client.publish(topic, payload, qos=qos, retain=False)
    result.wait_for_publish(timeout=5)
    if not result.is_published():
        raise RuntimeError(f"MQTT publish did not complete for {topic}")


def publish_json(
    client: Any,
    topic: str,
    payload: dict[str, Any],
    *,
    qos: int = 1,
) -> None:
    publish(client, topic, json.dumps(payload, separators=(",", ":")), qos=qos)


def pause(client: Any, seconds: float) -> None:
    if client is not None:
        time.sleep(seconds)


def pause_before_test(client: Any, enabled: bool, next_test: str) -> None:
    if not enabled:
        return
    print(
        f"\nPausing {TEST_PAUSE_SECONDS} seconds before next test: {next_test}",
        flush=True,
    )
    pause(client, TEST_PAUSE_SECONDS)


def publish_verse(client: Any, query: str, text: str) -> None:
    publish_json(
        client,
        VERSE_TOPIC,
        {"query": query, "text": text, "ts": time.time()},
    )


def publish_sentiment(
    client: Any,
    verse: str,
    classification: list[tuple[str, float]],
) -> None:
    publish_json(
        client,
        SENTIMENT_TOPIC,
        {
            "verse": verse,
            # The deliberately unsorted order verifies label-based lookup.
            "classification": [
                {"label": label, "score": score}
                for label, score in classification
            ],
            # These remain in the real schema but are deliberately ignored.
            "flare": 369,
            "bigjet": 0,
            "ts": time.time(),
        },
    )


def publish_flame(client: Any, valve: str, milliseconds: int) -> None:
    publish_json(
        client,
        FLAME_TOPIC,
        {"valve": valve, "ms": milliseconds},
        qos=0,
    )


def run_normal(client: Any, hold_seconds: float) -> None:
    text = "Silver watcher over sleeping stone, the bright night answers."
    print("\nNormal completion: joy/turquoise with an orchid love accent")
    publish_verse(client, "tell me about the moon", text)
    publish_sentiment(
        client,
        text,
        [
            ("fear", .04),
            ("love", .19),
            ("anger", .03),
            ("sadness", .05),
            ("joy", .64),
            ("surprise", .05),
        ],
    )
    publish_json(client, SPEAKING_TOPIC, {"text": text, "ts": time.time()})
    pause(client, hold_seconds)
    publish_json(client, DONE_TOPIC, {"ts": time.time()})
    print("Expected: one release/exhale, then a slow return to animated indigo.")


def run_emotions(
    client: Any,
    hold_seconds: float,
    with_flame_pulses: bool,
    pause_between_tests: bool,
) -> None:
    comparisons = [
        (
            "joy + love",
            "show me shared delight",
            "Joy and love rise together around the fire.",
            [
                ("fear", .03),
                ("love", .34),
                ("anger", .04),
                ("sadness", .03),
                ("joy", .51),
                ("surprise", .05),
            ],
        ),
        (
            "anger + surprise",
            "show me a sudden challenge",
            "Anger strikes as surprise breaks the stillness.",
            [
                ("love", .03),
                ("surprise", .33),
                ("sadness", .03),
                ("anger", .52),
                ("fear", .05),
                ("joy", .04),
            ],
        ),
        (
            "fear + sadness",
            "show me a shadowed memory",
            "Fear settles into the long blue weight of sadness.",
            [
                ("joy", .03),
                ("sadness", .39),
                ("surprise", .04),
                ("anger", .03),
                ("fear", .46),
                ("love", .05),
            ],
        ),
        (
            "balanced",
            "show me every feeling together",
            "Every feeling shares the fire for one breath.",
            [
                ("fear", .15),
                ("love", .18),
                ("anger", .15),
                ("sadness", .17),
                ("joy", .17),
                ("surprise", .18),
            ],
        ),
    ]

    print("\nEmotion comparisons: four contrasting six-score palettes")
    if with_flame_pulses:
        print("Flame overlay: flare 350 ms, then bigjet 700 ms, on every mix")
    for index, (name, query, text, classification) in enumerate(comparisons):
        print(f"\nComparison {index + 1}/{len(comparisons)}: {name}")
        publish_verse(client, query, text)
        publish_sentiment(client, text, classification)
        publish_json(client, SPEAKING_TOPIC, {"text": text, "ts": time.time()})
        if with_flame_pulses:
            before_flame = min(1, hold_seconds)
            between_flames = min(.75, max(0, hold_seconds - before_flame))
            pause(client, before_flame)
            publish_flame(client, "flare", 350)
            pause(client, between_flames)
            publish_flame(client, "bigjet", 700)
            pause(client, max(0, hold_seconds - before_flame - between_flames))
        else:
            pause(client, hold_seconds)
        publish_json(client, DONE_TOPIC, {"ts": time.time()})
        if index + 1 < len(comparisons):
            next_test = comparisons[index + 1][0]
            if pause_between_tests:
                pause_before_test(client, True, next_test)
            else:
                pause(client, DEFAULT_COMPARISON_GAP_SECONDS)

    print("Expected: four distinct palettes, each followed by a release.")


def run_watchdog(client: Any, watchdog_seconds: float) -> None:
    text = "Something waits beyond the edge of the firelight."
    print("\nMissing done: fear/teal with a cobalt sadness accent")
    publish(client, TIMEOUT_TOPIC, str(watchdog_seconds))
    publish_verse(client, "what waits in the dark", text)
    publish_sentiment(
        client,
        text,
        [
            ("joy", .03),
            ("sadness", .23),
            ("surprise", .06),
            ("anger", .05),
            ("fear", .59),
            ("love", .04),
        ],
    )
    publish_json(client, SPEAKING_TOPIC, {"text": text, "ts": time.time()})
    print("No done message will be sent; waiting for the scene watchdog...")
    pause(client, watchdog_seconds + 2)
    print("Expected: automatic release; releaseWasWatchdog should read 1.")
    publish(client, TIMEOUT_TOPIC, "30")


def run_races(client: Any) -> None:
    old_text = "Old roots remember rain beneath the fire."
    new_text = "New sparks climb through a violet midnight."
    print("\nRace handling: interrupted speech, stale done, and late sentiment")

    publish_verse(client, "tell me what the roots remember", old_text)
    publish_sentiment(
        client,
        old_text,
        [
            ("joy", .06),
            ("anger", .08),
            ("sadness", .63),
            ("love", .09),
            ("fear", .10),
            ("surprise", .04),
        ],
    )
    publish_json(client, SPEAKING_TOPIC, {"text": old_text, "ts": time.time()})
    pause(client, 1)

    publish_verse(client, "show me a new spark", new_text)
    pause(client, .5)
    publish_json(client, DONE_TOPIC, {"ts": time.time()})
    print("Expected: likely stale done is ignored; sceneMode remains 1 and doneArmed is 0.")

    publish_sentiment(
        client,
        old_text,
        [
            ("anger", .82),
            ("joy", .03),
            ("love", .03),
            ("surprise", .04),
            ("fear", .05),
            ("sadness", .03),
        ],
    )
    print("Expected: stale sentiment is rejected; lastSentimentMatched is 0.")

    # The real producers run independently, so speaking is allowed to precede
    # the matching sentiment result.
    publish_json(client, SPEAKING_TOPIC, {"text": new_text, "ts": time.time()})
    publish_sentiment(
        client,
        new_text,
        [
            ("fear", .04),
            ("love", .14),
            ("joy", .12),
            ("surprise", .59),
            ("anger", .07),
            ("sadness", .04),
        ],
    )
    print("Expected: matching late sentiment is accepted; lastSentimentMatched is 1.")
    pause(client, 2)
    publish_json(client, DONE_TOPIC, {"ts": time.time()})


def run_flame(client: Any) -> None:
    text = "The fire answers with a cool shadow between its breaths."
    print("\nFlame observation: valve pulses add visual ducking and cool rebounds")
    publish_verse(client, "how does the fire answer", text)
    publish_sentiment(
        client,
        text,
        [
            ("love", .44),
            ("surprise", .20),
            ("joy", .18),
            ("fear", .08),
            ("sadness", .07),
            ("anger", .03),
        ],
    )
    publish_json(client, SPEAKING_TOPIC, {"text": text, "ts": time.time()})
    pause(client, 1)

    publish_flame(client, "flare", 350)
    pause(client, .15)
    publish_flame(client, "flare", 700)
    pause(client, .2)
    publish_flame(client, "bigjet", 900)
    publish_flame(client, "poof", 180)
    print(
        "Expected: repeated flare extends its effect; bigjet and poof overlap "
        "independently, then leave blue/cyan afterglows."
    )
    pause(client, 4)
    publish_json(client, DONE_TOPIC, {"ts": time.time()})


def main() -> int:
    args = parse_args()
    client = connect(args)
    try:
        print(f"Selecting {args.layout} layout")
        publish(client, LAYOUT_TOPIC, args.layout)
        publish(client, BRIGHTNESS_TOPIC, ".45")
        publish(
            client,
            ANTICIPATION_TIMEOUT_TOPIC,
            str(args.anticipation_timeout_seconds),
        )
        publish(client, DECAY_TOPIC, str(args.decay_seconds))
        publish(
            client,
            SPIRAL_DIRECTION_TOPIC,
            "1" if args.spiral_center == "first" else "0",
        )
        pause(client, args.settle_seconds)

        if args.scenario == "both":
            scenarios = ("normal", "watchdog")
        elif args.scenario == "all":
            scenarios = ("normal", "emotions", "watchdog", "races", "flame")
        else:
            scenarios = (args.scenario,)

        scenario_labels = {
            "normal": "normal completion",
            "emotions": "emotion comparisons",
            "watchdog": "missing-done watchdog",
            "races": "race handling",
            "flame": "standalone flame observation",
        }
        for index, scenario in enumerate(scenarios):
            if index > 0:
                pause_before_test(
                    client,
                    args.pause_between_tests,
                    scenario_labels[scenario],
                )
            if scenario == "normal":
                run_normal(client, args.hold_seconds)
            elif scenario == "emotions":
                run_emotions(
                    client,
                    args.hold_seconds,
                    args.with_flame_pulses,
                    args.pause_between_tests,
                )
            elif scenario == "watchdog":
                run_watchdog(client, args.watchdog_seconds)
            elif scenario == "races":
                run_races(client)
            elif scenario == "flame":
                run_flame(client)
    finally:
        if client is not None:
            client.disconnect()
            client.loop_stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

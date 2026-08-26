from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from mqtt_pixelblaze_bridge import load_config


EXAMPLE = ROOT / "examples" / "emotional-fire"
PATTERNS = (
    ROOT / "pixelblaze" / "emotional_fire_spiral.js",
    ROOT / "pixelblaze" / "emotional_fire_unordered.js",
)
SCENE_INPUTS = {
    "inputAnger",
    "inputJoy",
    "inputLove",
    "inputSurprise",
    "inputFear",
    "inputSadness",
    "inputVerseHash",
    "inputSentimentVerseHash",
    "inputFlamePulseMs",
    "inputFlameValve",
    "sentimentTrigger",
    "verseTrigger",
    "speakingTrigger",
    "doneTrigger",
    "flamePulseTrigger",
    "speakingTimeoutSeconds",
    "anticipationTimeoutSeconds",
    "decaySeconds",
}
SCENE_DIAGNOSTICS = {
    "sceneMode",
    "emotionLevel",
    "releaseWasWatchdog",
    "doneArmed",
    "lastSentimentMatched",
    "lastFlameValve",
}


def published_json(stdout: str, topic: str) -> list[dict[str, object]]:
    prefix = f"{topic} <- "
    return [
        json.loads(line.removeprefix(prefix))
        for line in stdout.splitlines()
        if line.startswith(prefix)
    ]


def test_emotional_fire_configuration_stages_every_emotion_before_trigger() -> None:
    config = load_config(EXAMPLE / "bridge.local.json")
    sentiment = [
        binding
        for binding in config.bindings
        if binding.topic == "bush/pipeline/sentiment/result"
    ]

    assert [
        binding.array_lookup.match_value
        for binding in sentiment[:-2]
        if binding.array_lookup is not None
    ] == ["anger", "joy", "love", "surprise", "fear", "sadness"]
    assert [binding.variable for binding in sentiment[:-2]] == [
        "inputAnger",
        "inputJoy",
        "inputLove",
        "inputSurprise",
        "inputFear",
        "inputSadness",
    ]
    assert sentiment[-2].value_path == ("verse",)
    assert sentiment[-2].transform == "text_hash"
    assert sentiment[-2].variable == "inputSentimentVerseHash"
    assert sentiment[-1].command == "trigger"
    assert sentiment[-1].variable == "sentimentTrigger"


def test_emotional_fire_configuration_stages_event_metadata_before_triggers() -> None:
    config = load_config(EXAMPLE / "bridge.local.json")

    verse = [
        binding
        for binding in config.bindings
        if binding.topic == "bush/pipeline/t2v/verse"
    ]
    assert verse[0].value_path == ("text",)
    assert verse[0].transform == "text_hash"
    assert verse[0].variable == "inputVerseHash"
    assert verse[1].command == "trigger"
    assert verse[1].variable == "verseTrigger"

    flame = [
        binding
        for binding in config.bindings
        if binding.topic == "bush/flame/pulse"
    ]
    assert flame[0].value_path == ("ms",)
    assert flame[0].variable == "inputFlamePulseMs"
    assert flame[1].value_path == ("valve",)
    assert flame[1].value_map == {"flare": 1.0, "bigjet": 2.0, "poof": 3.0}
    assert flame[1].variable == "inputFlameValve"
    assert flame[2].command == "trigger"
    assert flame[2].variable == "flamePulseTrigger"


def test_both_pixelblaze_patterns_export_the_bridge_contract() -> None:
    for path in PATTERNS:
        source = path.read_text(encoding="utf-8")
        exports = set(re.findall(r"^export var ([A-Za-z][A-Za-z0-9]*)", source, re.MULTILINE))

        assert SCENE_INPUTS | SCENE_DIAGNOSTICS <= exports
        assert "export var flare" not in source
        assert "export var bigjet" not in source
        assert "export var speakingTimeoutSeconds = 30" in source
        assert (
            "activeVerseHash != 0 && "
            "inputSentimentVerseHash == activeVerseHash"
        ) in source
        assert "function triggerAfter(candidate, reference)" in source
        assert "doneArmed && doneAfterVerse && doneAfterSpeaking" in source
        assert "max(flareOpenSeconds, duration)" in source
        assert "export function beforeRender(delta)" in source
        assert "export function render(index)" in source
        assert "var frameHue = array(pixelCount)" in source
        assert "var frameSaturation = array(pixelCount)" in source
        assert "var frameValue = array(pixelCount)" in source
        assert (
            "for (var frameIndex = 0; frameIndex < pixelCount; frameIndex++)"
            in source
        )
        assert "calculatePixel(frameIndex)" in source
        assert "function calculatePixel(index)" in source
        assert (
            "hsv(frameHue[index], frameSaturation[index], frameValue[index])"
            in source
        )
        assert "basePhase = frac(basePhase + seconds / 14)" in source
        assert "emberTwinkle" in source


def test_smoke_publisher_dry_run_exercises_all_message_paths() -> None:
    result = subprocess.run(
        [
            sys.executable,
            str(EXAMPLE / "smoke_test.py"),
            "--dry-run",
            "--scenario",
            "all",
            "--layout",
            "unordered",
        ],
        check=True,
        capture_output=True,
        text=True,
    )

    assert "bush/pipeline/t2v/verse <-" in result.stdout
    assert "bush/pipeline/sentiment/result <-" in result.stdout
    assert "bush/pipeline/tts/speaking <-" in result.stdout
    assert "bush/pipeline/tts/done <-" in result.stdout
    assert 'bush/flame/pulse <- {"valve":"flare","ms":350}' in result.stdout
    assert 'bush/flame/pulse <- {"valve":"bigjet","ms":900}' in result.stdout
    assert "No done message will be sent" in result.stdout
    assert "likely stale done is ignored" in result.stdout
    assert "stale sentiment is rejected" in result.stdout


def test_emotion_comparison_scenario_publishes_four_mixes_without_flames() -> None:
    result = subprocess.run(
        [
            sys.executable,
            str(EXAMPLE / "smoke_test.py"),
            "--dry-run",
            "--scenario",
            "emotions",
            "--layout",
            "unordered",
        ],
        check=True,
        capture_output=True,
        text=True,
    )

    sentiments = published_json(
        result.stdout,
        "bush/pipeline/sentiment/result",
    )
    dominant_pairs = []
    for sentiment in sentiments:
        classification = sentiment["classification"]
        assert isinstance(classification, list)
        ranked = sorted(
            classification,
            key=lambda item: item["score"],
            reverse=True,
        )
        dominant_pairs.append(
            [
                (ranked[0]["label"], ranked[0]["score"]),
                (ranked[1]["label"], ranked[1]["score"]),
            ]
        )

    assert dominant_pairs == [
        [("joy", 0.51), ("love", 0.34)],
        [("anger", 0.52), ("surprise", 0.33)],
        [("fear", 0.46), ("sadness", 0.39)],
        [("love", 0.18), ("surprise", 0.18)],
    ]
    assert len(published_json(result.stdout, "bush/pipeline/t2v/verse")) == 4
    assert len(published_json(result.stdout, "bush/pipeline/tts/speaking")) == 4
    assert len(published_json(result.stdout, "bush/pipeline/tts/done")) == 4
    assert "bush/flame/pulse <-" not in result.stdout


def test_emotion_flame_overlay_requires_explicit_live_permission() -> None:
    result = subprocess.run(
        [
            sys.executable,
            str(EXAMPLE / "smoke_test.py"),
            "--scenario",
            "emotions",
            "--with-flame-pulses",
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 2
    assert "can actuate a connected relay" in result.stderr
    assert "--allow-flame-pulses" in result.stderr


def test_emotion_flame_overlay_repeats_one_comparable_pulse_pair_per_mix() -> None:
    result = subprocess.run(
        [
            sys.executable,
            str(EXAMPLE / "smoke_test.py"),
            "--dry-run",
            "--scenario",
            "emotions",
            "--with-flame-pulses",
        ],
        check=True,
        capture_output=True,
        text=True,
    )

    assert published_json(result.stdout, "bush/flame/pulse") == [
        {"valve": "flare", "ms": 350},
        {"valve": "bigjet", "ms": 700},
    ] * 4


def test_emotion_flame_overlay_flag_rejects_unrelated_scenarios() -> None:
    result = subprocess.run(
        [
            sys.executable,
            str(EXAMPLE / "smoke_test.py"),
            "--dry-run",
            "--scenario",
            "normal",
            "--with-flame-pulses",
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 2
    assert "requires --scenario emotions or all" in result.stderr


def test_pause_between_tests_schedules_thirty_seconds_between_all_cases() -> None:
    result = subprocess.run(
        [
            sys.executable,
            str(EXAMPLE / "smoke_test.py"),
            "--dry-run",
            "--scenario",
            "all",
            "--pause-between-tests",
        ],
        check=True,
        capture_output=True,
        text=True,
    )

    pause_message = "Pausing 30 seconds before next test:"
    assert result.stdout.count(pause_message) == 7
    assert f"{pause_message} emotion comparisons" in result.stdout
    assert f"{pause_message} anger + surprise" in result.stdout
    assert f"{pause_message} missing-done watchdog" in result.stdout


def test_smoke_publisher_requires_explicit_permission_for_live_flame_pulses() -> None:
    result = subprocess.run(
        [
            sys.executable,
            str(EXAMPLE / "smoke_test.py"),
            "--scenario",
            "flame",
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 2
    assert "can actuate a connected relay" in result.stderr
    assert "--allow-flame-pulses" in result.stderr

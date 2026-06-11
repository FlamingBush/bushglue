#!/usr/bin/env python3
"""
Text-to-speech service for Bush Glue.
Subscribes to bush/pipeline/t2v/verse and speaks each verse aloud via espeak-ng.
Queues verses so rapid-fire messages don't overlap; drops stale items if the
queue backs up so playback stays roughly in sync with the pipeline.

Accepts runtime output device changes via bush/audio/tts/set-device {"device": <str|null>}.
"""
import json
import os
import pathlib
import queue
import subprocess
import threading
import time

import paho.mqtt.client as mqtt

from bushutil import (make_logger, run_mqtt_service, load_audio_device,
                      save_audio_device, load_setting, save_setting,
                      build_sox_effects)

# ── config ─────────────────────────────────────────────────────────────────
TOPIC_VERSE         = "bush/pipeline/t2v/verse"
TOPIC_SPEAKING      = "bush/pipeline/tts/speaking"
TOPIC_DONE          = "bush/pipeline/tts/done"
TOPIC_SET_DEVICE    = "bush/audio/tts/set-device"
TOPIC_DEVICE_STATUS = "bush/audio/tts/device"
TOPIC_SET_CLARITY   = "bush/audio/tts/set-clarity"
TOPIC_CLARITY       = "bush/audio/tts/clarity"

# Extra silence after sox finishes before signalling done (reverb tail)
DONE_TAIL_S = 0.5

# Failsafe: kill sox if it hasn't finished within this many seconds
TTS_TIMEOUT_S = 60

# Engine selection: 'espeak' (default, current behavior) or 'piper' (neural).
# espeak-ng's voice flags now live in engines/espeak.py; piper voice config
# is loaded by engines/piper.py from the .onnx.json sidecar at startup.
_REPO_ROOT = pathlib.Path(__file__).resolve().parents[4]  # bushglue/
TTS_ENGINE_NAME = os.environ.get("TTS_ENGINE", "espeak").lower()
PIPER_VOICE_PATH = os.environ.get(
    "PIPER_VOICE",
    str(_REPO_ROOT / "data" / "piper-voices" / "en_GB-alan-medium.onnx"),
)

# sox effects applied after the engine's PCM (clarity=0 defaults):
#   gain -8        headroom before effects to prevent clipping
#   pitch -250     ~2.5 semitones down — deep but not subterranean
#   reverb 65 12 100 100 28 3
#     65%  reverberance  — long open tail, not dense
#     12%  HF-damping    — stay bright; rock/sky reflect high freqs well
#     100% room-scale    — vast open space
#     100% stereo-depth  — wide horizon
#     28ms pre-delay     — sound crossing distance before cliff echo returns
#     3dB  wet-gain      — present but not drowning the voice
# SOX_EFFECTS = ["gain", "-8", "pitch", "-250", "reverb", "65", "12", "100", "100", "28", "3"]
# (now generated dynamically via build_sox_effects(_tts_clarity))

# Drop queued verses beyond this depth so we never fall minutes behind
QUEUE_MAX = 2

# Current TTS output device: None → sox default (-d), str → ALSA device name
_tts_device: str | None = load_audio_device("tts")  # restore last saved device (or None)

# Current clarity level (0 = dramatic/default, 100 = most intelligible)
_tts_clarity: int = load_setting("tts_clarity", 0)

_state_lock = threading.Lock()  # guards _tts_device + _tts_clarity


def _sox_cmd(sample_rate: int) -> list[str]:
    """Build the sox command for raw int16 LE mono PCM input at the given rate.

    Args:
        sample_rate: Hz of the input PCM (engine.sample_rate from synthesis result).
    """
    with _state_lock:
        dev = _tts_device
        clarity = _tts_clarity
    if dev is None:
        output_args = ["-d"]
    else:
        output_args = ["-t", "alsa", dev]
    return [
        "sox", "-q",
        "-t", "raw", "-r", str(sample_rate), "-e", "signed", "-b", "16", "-c", "1", "-",
    ] + output_args + build_sox_effects(clarity)


log = make_logger("tts-service")


def _build_engine():
    """Construct the TTS engine selected by TTS_ENGINE env var.

    Defaults to espeak (current behavior). 'piper' switches to neural TTS.
    """
    if TTS_ENGINE_NAME == "piper":
        from bush_tts.engines.piper import PiperEngine
        log(f"using Piper engine (voice={PIPER_VOICE_PATH})")
        return PiperEngine(voice_path=PIPER_VOICE_PATH)
    elif TTS_ENGINE_NAME == "espeak":
        from bush_tts.engines.espeak import EspeakEngine
        log("using espeak engine")
        return EspeakEngine()
    else:
        raise RuntimeError(
            f"Unknown TTS_ENGINE={TTS_ENGINE_NAME!r}; must be 'espeak' or 'piper'"
        )


# ── speech worker ───────────────────────────────────────────────────────────
speech_queue: queue.Queue[str | None] = queue.Queue(maxsize=QUEUE_MAX)
_current_procs: list[subprocess.Popen] = []
_proc_lock = threading.Lock()
_mqttc: mqtt.Client | None = None   # set after connect
_engine = None                      # set at startup in main()


def _kill_current():
    """Kill the in-progress sox process to interrupt playback immediately.

    The engine adapter runs synthesize() synchronously inside the worker, so
    there is no separate engine subprocess to kill here — only sox.
    """
    with _proc_lock:
        for p in _current_procs:
            if p.poll() is None:
                p.kill()
        _current_procs.clear()


def _publish(topic: str, payload: dict):
    """Publish from the worker thread; a no-op before the first connect."""
    if _mqttc:
        _mqttc.publish(topic, json.dumps(payload))


def _publish_done():
    _publish(TOPIC_DONE, {"ts": time.time()})


def _speak_worker():
    """Runs in a background thread; pulls verses and speaks them one at a time."""
    while True:
        text = speech_queue.get()
        if text is None:
            break
        log(f"Speaking: {text[:80]!r}")
        _publish(TOPIC_SPEAKING, {"text": text, "ts": time.time()})

        # ALSA device-share gating: give STT time to release the capture
        # interface before sox opens playback. STT inner loop polls
        # audio_queue with 0.5s timeout, so worst-case teardown is ~500ms +
        # kill latency. Use 600ms to cover it.
        with _state_lock:
            dev = _tts_device
        if dev is not None and (dev.startswith("hw:") or dev.startswith("plughw:")):
            time.sleep(0.6)

        timed_out = False
        sox_failed = False
        was_killed = False
        try:
            # Synthesize: call engine. Errors here mean the engine failed; we
            # log, publish done (so the pipeline doesn't block), and skip this
            # verse.
            try:
                synth = _engine.synthesize(text)
            except Exception as e:
                log(f"engine.synthesize error: {e}")
                _publish_done()
                speech_queue.task_done()
                continue

            audio_pcm = synth["audio_pcm"]
            sr = synth["sample_rate"]
            if not audio_pcm:
                log("engine returned empty audio; skipping")
                _publish_done()
                speech_queue.task_done()
                continue

            sox = subprocess.Popen(
                _sox_cmd(sr),
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
            )
            with _proc_lock:
                _current_procs.append(sox)

            # Feed all PCM bytes; close stdin so sox knows EOF.
            try:
                sox.stdin.write(audio_pcm)
                sox.stdin.close()
            except (BrokenPipeError, OSError):
                pass

            try:
                sox.wait(timeout=TTS_TIMEOUT_S)
            except subprocess.TimeoutExpired:
                log(f"sox timed out after {TTS_TIMEOUT_S}s — killing")
                timed_out = True
                sox.kill()
                sox.wait()

            sox_rc = sox.returncode
            sox_err = (sox.stderr.read().decode(errors="replace").strip()
                       if sox.stderr else "")
            if sox_err and "can't encode 0-bit" not in sox_err:
                log(f"sox stderr (rc={sox_rc}): {sox_err}")
            # rc=-9: killed via _kill_current (SIGKILL) or timeout — still publish done
            # rc=other non-zero: sox device/format error — skip done to avoid false gate clear
            was_killed = sox_rc == -9 and not timed_out
            sox_failed = sox_rc not in (0, None) and not was_killed and not timed_out

            if timed_out:
                log(f"sox timed out (rc={sox_rc}) — publishing done to unblock pipeline")
            elif sox_failed:
                log(f"sox failed (rc={sox_rc}) — skipping done signal")

            with _proc_lock:
                _current_procs.clear()
            if not was_killed and not timed_out and not sox_failed:
                time.sleep(DONE_TAIL_S)
            if not sox_failed:
                _publish_done()
        except Exception as e:
            log(f"speak error: {e}")
        speech_queue.task_done()


def _enqueue(text: str):
    """Add verse to queue, dropping oldest if full."""
    while True:
        try:
            speech_queue.put_nowait(text)
            return
        except queue.Full:
            try:
                dropped = speech_queue.get_nowait()
                log(f"Queue full — dropped: {dropped[:40]!r}")
                speech_queue.task_done()
            except queue.Empty:
                pass  # worker drained it between put and get; retry the put


def _interrupt_and_enqueue(text: str):
    """Interrupt current speech and drain queue before enqueuing new verse."""
    _kill_current()
    while not speech_queue.empty():
        try:
            speech_queue.get_nowait()
            speech_queue.task_done()
        except queue.Empty:
            break
    _enqueue(text)


# ── MQTT ────────────────────────────────────────────────────────────────────
def on_message(client, userdata, msg):
    global _tts_device, _tts_clarity
    if msg.topic == TOPIC_VERSE:
        try:
            data = json.loads(msg.payload)
            text = data.get("text", "").strip()
            if not text:
                return
            first_para = text.split("\n\n")[0]
            clean = " ".join(line.strip() for line in first_para.splitlines() if line.strip())
            _interrupt_and_enqueue(clean)
        except Exception as e:
            log(f"Message error: {e}")
    elif msg.topic == TOPIC_SET_DEVICE:
        try:
            data = json.loads(msg.payload)
            raw = data.get("device")   # None or str
            dev = str(raw) if raw is not None else None
            with _state_lock:
                _tts_device = dev
            save_audio_device("tts", dev)
            log(f"Output device set to: {dev!r}")
            client.publish(TOPIC_DEVICE_STATUS,
                           json.dumps({"device": dev, "status": "ok"}), retain=True)
        except Exception as e:
            log(f"set-device error: {e}")
    elif msg.topic == TOPIC_SET_CLARITY:
        try:
            data = json.loads(msg.payload)
            raw = int(data.get("clarity", 0))
            clamped = max(0, min(100, raw))
            with _state_lock:
                _tts_clarity = clamped
            save_setting("tts_clarity", clamped)
            log(f"Clarity set to: {clamped}")
            client.publish(TOPIC_CLARITY,
                           json.dumps({"clarity": clamped, "status": "ok"}), retain=True)
        except Exception as e:
            log(f"set-clarity error: {e}")


def main():
    global _engine
    _engine = _build_engine()

    threading.Thread(target=_speak_worker, daemon=True).start()

    def _post_connect(client):
        global _mqttc
        _mqttc = client
        with _state_lock:
            dev = _tts_device
            clarity = _tts_clarity
        client.publish(TOPIC_DEVICE_STATUS, json.dumps({"device": dev}), retain=True)
        client.publish(TOPIC_CLARITY, json.dumps({"clarity": clarity}), retain=True)

    def _on_shutdown():
        speech_queue.put(None)
        _kill_current()

    run_mqtt_service("tts-service", [TOPIC_VERSE, TOPIC_SET_DEVICE, TOPIC_SET_CLARITY],
                     on_message, on_connect=_post_connect, on_shutdown=_on_shutdown)


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""
Speech-to-Text MQTT publisher.
Reads from microphone using Vosk, publishes final transcriptions to
bush/pipeline/stt/transcript as {"text": "...", "ts": <epoch>}.

Mutes itself while TTS is speaking (bush/pipeline/tts/speaking) and
unmutes on bush/pipeline/tts/done, resetting the Vosk recognizer so
any partial state from hearing TTS speech is discarded.

Accepts runtime device changes via bush/audio/stt/set-device {"device": <int|str>}.

Accuracy pipeline (applied in order to every final transcript):
  1. SoX highpass filter (200 Hz) on the capture stream — removes generator/fire LF noise
  2. VAD gating (webrtcvad) — auto-finalizes after STT_VAD_SILENCE_MS of silence
  3. Confidence threshold — drops low-confidence Vosk results (STT_CONFIDENCE)
  4. LLM post-correction via Ollama — optional domain-aware cleanup (STT_LLM_CORRECT=1)

Environment variables:
  STT_DIR                 path to speech-to-text repo (required)
  STT_MODEL               path to Vosk model dir (default: $STT_DIR/models/en-us)
  STT_DEVICE              audio device name or index (default: persisted config)
  STT_VAD_AGGRESSIVENESS  webrtcvad aggressiveness 0-3 (default: 2)
  STT_VAD_SILENCE_MS      ms of silence before auto-finalize (default: 810)
  STT_CONFIDENCE          min mean word confidence 0.0-1.0 (default: 0.6)
  STT_LLM_CORRECT         set to 1 to enable Ollama post-correction (default: off)
  STT_LLM_MODEL           Ollama model for correction (default: qwen3:1.7b)
  OLLAMA_URL              Ollama base URL (default: http://localhost:11434)
"""
import collections
import json
import os
import pathlib
import queue
import random
import subprocess as _subprocess
import sys
import threading
import time

import paho.mqtt.client as mqtt
import webrtcvad

from bushutil import get_mqtt_broker, load_audio_device, save_audio_device

# ── paths / device ─────────────────────────────────────────────────────────
STT_DIR = os.environ.get("STT_DIR")
if not STT_DIR:
    sys.exit(
        "[stt-service] FATAL: STT_DIR env var is not set. "
        "Set it to the path of the speech-to-text repo "
        "(e.g. STT_DIR=/home/ubuntu/repos/speech-to-text)."
    )
MODEL_PATH = os.environ.get("STT_MODEL", f"{STT_DIR}/models/en-us")
_dev = os.environ.get("STT_DEVICE")
if _dev:
    STT_DEVICE = int(_dev) if _dev.isdigit() else _dev  # int index or string name
else:
    STT_DEVICE = load_audio_device("stt")  # restore last saved device (or None)
SAMPLE_RATE = 16000

# ── accuracy pipeline config ───────────────────────────────────────────────
VAD_AGGRESSIVENESS  = int(os.environ.get("STT_VAD_AGGRESSIVENESS", "2"))    # 0–3
VAD_SILENCE_MS      = int(os.environ.get("STT_VAD_SILENCE_MS", "810"))      # ms of silence → finalize
VAD_FRAME_MS        = 30                                                      # webrtcvad frame size
VAD_FRAME_BYTES     = int(SAMPLE_RATE * VAD_FRAME_MS / 1000) * 2            # 960 bytes at 16 kHz
VAD_SILENCE_FRAMES  = VAD_SILENCE_MS // VAD_FRAME_MS                         # frames before finalize
VAD_SPEECH_FRAMES   = 3                                                       # frames before onset

CONFIDENCE_THRESHOLD = float(os.environ.get("STT_CONFIDENCE", "0.6"))

LLM_CORRECT  = os.environ.get("STT_LLM_CORRECT", "0") == "1"
LLM_MODEL    = os.environ.get("STT_LLM_MODEL", "qwen3:1.7b")
OLLAMA_URL   = os.environ.get("OLLAMA_URL", "http://localhost:11434")

# ── Fallback phrases ───────────────────────────────────────────────────────
# Used when a manual force-finalize produces no speech. Cycles through all
# phrases before repeating (shuffled reservoir). VAD-triggered silence does
# NOT use these — they're reserved for intentional finalize requests.
FALLBACK_PHRASES = [
    "what is the fire",
    "speak of the light",
    "what burns in the darkness",
    "tell me of the wilderness",
    "who tends the flame",
    "what is the meaning of the desert",
    "speak of the beginning",
    "what lives in the smoke",
    "where does the fire come from",
    "tell me of the burning bush",
    "what is the voice in the wilderness",
    "speak of water and flame",
    "what is revealed by fire",
    "tell me of the night",
    "what rises from the ash",
]
_fallback_iter: list[str] = []


def _next_fallback() -> str:
    global _fallback_iter
    if not _fallback_iter:
        _fallback_iter = random.sample(FALLBACK_PHRASES, len(FALLBACK_PHRASES))
    return _fallback_iter.pop()


# ── MQTT ───────────────────────────────────────────────────────────────────
TOPIC_TRANSCRIPT      = "bush/pipeline/stt/transcript"
TOPIC_PARTIAL         = "bush/pipeline/stt/partial"
TOPIC_TTS_SPEAKING    = "bush/pipeline/tts/speaking"
TOPIC_TTS_DONE        = "bush/pipeline/tts/done"
TOPIC_SET_DEVICE      = "bush/audio/stt/set-device"
TOPIC_DEVICE_STATUS   = "bush/audio/stt/device"
TOPIC_FORCE_FINALIZE  = "bush/pipeline/stt/force-finalize"
TOPIC_PIPELINE_PING   = "bush/pipeline/ping"
TOPIC_PIPELINE_PONG   = "bush/pipeline/pong"
MQTT_PORT = 1883


def log(msg: str):
    print(f"[stt-service] {msg}", flush=True)


_AUDIO_RETRY_INTERVAL = 10  # seconds between device-ready checks


# ── audio device helpers ───────────────────────────────────────────────────

def _is_alsa_device(device) -> bool:
    s = str(device)
    return s.startswith("hw:") or s.startswith("plughw:")


def _pa_source_present(device) -> bool:
    try:
        result = _subprocess.run(
            ["pactl", "list", "short", "sources"],
            capture_output=True, text=True, timeout=5,
        )
        return str(device) in result.stdout
    except Exception:
        return False


def _alsa_device_present(device) -> bool:
    s = str(device)
    card = s.split(":")[1].split(",")[0] if ":" in s else s
    path = f"/proc/asound/card{card}" if card.isdigit() else f"/proc/asound/{card}"
    return pathlib.Path(path).exists()


def _wait_for_audio(device) -> None:
    if _is_alsa_device(device):
        def check() -> bool: return _alsa_device_present(device)
    else:
        def check() -> bool: return _pa_source_present(device)
    while not check():
        log(f"Audio source {device!r} not yet available — retrying in {_AUDIO_RETRY_INTERVAL}s...")
        time.sleep(_AUDIO_RETRY_INTERVAL)


# ── LLM post-correction ────────────────────────────────────────────────────

def _llm_correct(text: str) -> str:
    """Post-correct a Vosk transcript via Ollama. Falls back to original on any error."""
    try:
        import urllib.request
        payload = json.dumps({
            "model": LLM_MODEL,
            "prompt": (
                "Fix this speech-to-text output from a noisy outdoor art installation. "
                "The speaker is asking about fire, scripture, wilderness, or the divine. "
                "Return only the corrected text, nothing else.\n\n"
                f"Raw: {text}"
            ),
            "stream": False,
        }).encode()
        req = urllib.request.Request(
            f"{OLLAMA_URL}/api/generate",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=3.0) as resp:
            corrected = json.loads(resp.read()).get("response", "").strip()
        if corrected:
            log(f"LLM corrected: {text!r} → {corrected!r}")
            return corrected
        return text
    except Exception as e:
        log(f"LLM correction skipped ({e}) — using raw transcript")
        return text


# ── capture pipeline ───────────────────────────────────────────────────────

_SOX_HIGHPASS = [
    "sox", "-t", "raw", "-r", str(SAMPLE_RATE), "-e", "signed", "-b", "16", "-c", "1", "-",
    "-t", "raw", "-",
    "highpass", "200", "gain", "3",
]


def _open_capture(device) -> tuple:
    """
    Open the capture pipeline: capture_proc | sox_filter_proc.
    Returns (capture_proc, filter_proc).
    filter_proc.stdout is what should be read for audio.
    """
    if _is_alsa_device(device):
        log(f"Opening ALSA device {device!r} at {SAMPLE_RATE} Hz (+ highpass filter)...")
        capture_proc = _subprocess.Popen(
            ["arecord", "-D", str(device),
             "-f", "S16_LE", "-c", "1", f"-r{SAMPLE_RATE}", "-t", "raw"],
            stdout=_subprocess.PIPE,
            stderr=_subprocess.DEVNULL,
        )
    else:
        log(f"Opening PA source {device!r} at {SAMPLE_RATE} Hz (+ highpass filter)...")
        capture_proc = _subprocess.Popen(
            ["parec", "--device", str(device),
             "--format=s16le", f"--rate={SAMPLE_RATE}", "--channels=1"],
            stdout=_subprocess.PIPE,
            stderr=_subprocess.DEVNULL,
        )
    filter_proc = _subprocess.Popen(
        _SOX_HIGHPASS,
        stdin=capture_proc.stdout,
        stdout=_subprocess.PIPE,
        stderr=_subprocess.DEVNULL,
    )
    if capture_proc.stdout is not None:
        capture_proc.stdout.close()  # allow capture_proc to receive SIGPIPE if filter exits
    return capture_proc, filter_proc


def _close_capture(capture_proc, filter_proc) -> None:
    for proc in (filter_proc, capture_proc):
        if proc is not None:
            try:
                proc.kill()
                proc.wait()
            except Exception:
                pass


# ── VAD state ──────────────────────────────────────────────────────────────

def _make_vad_state() -> dict:
    return {
        "buf":      b"",
        "history":  collections.deque(maxlen=max(VAD_SILENCE_FRAMES, VAD_SPEECH_FRAMES)),
        "speaking": False,
    }


def _vad_process(vad: webrtcvad.Vad, chunk: bytes, state: dict) -> bool:
    """
    Feed *chunk* through VAD. Returns True if the service should auto-finalize
    (speech→silence transition detected).
    Mutates *state* in place.
    """
    state["buf"] += chunk
    finalize = False

    while len(state["buf"]) >= VAD_FRAME_BYTES:
        frame = state["buf"][:VAD_FRAME_BYTES]
        state["buf"] = state["buf"][VAD_FRAME_BYTES:]
        try:
            is_speech = vad.is_speech(frame, SAMPLE_RATE)
        except Exception:
            is_speech = True  # fail open — assume speech on VAD error

        state["history"].append(is_speech)

        if not state["speaking"]:
            recent = list(state["history"])[-VAD_SPEECH_FRAMES:]
            if len(recent) >= VAD_SPEECH_FRAMES and all(recent):
                state["speaking"] = True
                log("VAD: speech onset")
        else:
            recent = list(state["history"])
            if (len(recent) >= VAD_SILENCE_FRAMES
                    and not any(recent[-VAD_SILENCE_FRAMES:])):
                state["speaking"] = False
                log(f"VAD: {VAD_SILENCE_MS}ms silence — auto-finalizing")
                finalize = True

    return finalize


def main():
    broker = get_mqtt_broker()
    log(f"MQTT broker: {broker}:{MQTT_PORT}")

    # ── startup validation ─────────────────────────────────────────────────
    log(f"STT_DIR:             {STT_DIR}")
    log(f"MODEL_PATH:          {MODEL_PATH}")
    log(f"STT_DEVICE:          {STT_DEVICE!r}")
    log(f"VAD aggressiveness:  {VAD_AGGRESSIVENESS}  silence: {VAD_SILENCE_MS}ms")
    log(f"Confidence threshold:{CONFIDENCE_THRESHOLD}")
    log(f"LLM post-correction: {'on (' + LLM_MODEL + ')' if LLM_CORRECT else 'off'}")

    if not pathlib.Path(MODEL_PATH).exists():
        sys.exit(
            f"[stt-service] FATAL: Vosk model not found at {MODEL_PATH!r}. "
            "Download it and place it there, or set the STT_MODEL env var."
        )

    if STT_DEVICE is None:
        log(
            "WARNING: No STT_DEVICE configured and no saved device found. "
            "Service will loop in _wait_for_audio() indefinitely. "
            "Set STT_DEVICE env var or publish to bush/audio/stt/set-device."
        )

    # ── mute gate ──────────────────────────────────────────────────────────
    MUTE_TIMEOUT_S = 30
    muted = threading.Event()
    reset_recognizer = threading.Event()
    force_finalize = threading.Event()
    manual_finalize = threading.Event()  # set only on MQTT trigger, not VAD
    _mute_timer: list[threading.Timer | None] = [None]

    # ── device change ──────────────────────────────────────────────────────
    device_change = threading.Event()
    next_device = [STT_DEVICE]
    _current_device = [STT_DEVICE]  # list wrapper for cross-thread read safety

    # ── ALSA TTS pause/resume ──────────────────────────────────────────────
    tts_pause  = threading.Event()
    tts_resume = threading.Event()

    def on_tts_done():
        if _mute_timer[0] is not None:
            _mute_timer[0].cancel()
            _mute_timer[0] = None
        muted.clear()
        reset_recognizer.set()
        tts_pause.clear()
        tts_resume.set()
        log("Unmuting STT (TTS done)")

    def on_tts_speaking():
        if not muted.is_set():
            log("Muting STT (TTS speaking)")
        muted.set()
        if _mute_timer[0] is not None:
            _mute_timer[0].cancel()
        t = threading.Timer(MUTE_TIMEOUT_S, on_tts_done)
        t.daemon = True
        t.start()
        _mute_timer[0] = t
        if _is_alsa_device(_current_device[0]):
            tts_resume.clear()
            tts_pause.set()

    # ── MQTT setup ─────────────────────────────────────────────────────────
    # Stable client ID + clean_session=False so QoS 1 messages queued while
    # briefly disconnected (e.g. tts/speaking) are delivered on reconnect.
    mqttc = mqtt.Client(
        mqtt.CallbackAPIVersion.VERSION2,
        client_id="bush-stt",
        clean_session=False,
    )

    def on_message(client, userdata, msg):
        if msg.topic == TOPIC_TTS_SPEAKING:
            on_tts_speaking()
        elif msg.topic == TOPIC_TTS_DONE:
            on_tts_done()
        elif msg.topic == TOPIC_FORCE_FINALIZE:
            log("Force-finalize requested (manual).")
            manual_finalize.set()
            force_finalize.set()
        elif msg.topic == TOPIC_PIPELINE_PING:
            client.publish(TOPIC_PIPELINE_PONG, "")
        elif msg.topic == TOPIC_SET_DEVICE:
            try:
                data = json.loads(msg.payload)
                raw = data.get("device")
                if raw is None:
                    return
                dev = int(raw) if str(raw).lstrip("-").isdigit() else str(raw)
                log(f"Device change requested: {dev!r}")
                next_device[0] = dev
                save_audio_device("stt", dev)
                device_change.set()
            except Exception as e:
                log(f"set-device error: {e}")

    def on_connect(client, userdata, flags, reason_code, properties):
        # QoS 1 on mute/unmute — loss causes feedback loop or permanent mute.
        # Re-subscribe on every connect (handles reconnects after network drops).
        client.subscribe(TOPIC_TTS_SPEAKING, qos=1)
        client.subscribe(TOPIC_TTS_DONE, qos=1)
        client.subscribe(TOPIC_SET_DEVICE)
        client.subscribe(TOPIC_FORCE_FINALIZE)
        client.subscribe(TOPIC_PIPELINE_PING)
        client.publish(TOPIC_DEVICE_STATUS,
                       json.dumps({"device": next_device[0]}), retain=True)

    mqttc.on_connect = on_connect
    mqttc.on_message = on_message
    mqttc.connect(broker, MQTT_PORT, 60)
    mqttc.loop_start()
    log("MQTT connected.")

    # ── Vosk setup ─────────────────────────────────────────────────────────
    sys.path.insert(0, STT_DIR)
    from transcriber import SpeechToText
    from vosk import KaldiRecognizer

    log("Vosk model loading...")
    stt = SpeechToText(model_path=MODEL_PATH, sample_rate=SAMPLE_RATE)
    log("Vosk model loaded. Entering audio loop.")

    # ── VAD setup ──────────────────────────────────────────────────────────
    vad = webrtcvad.Vad(VAD_AGGRESSIVENESS)

    # ── restartable audio loop ─────────────────────────────────────────────
    CHUNK = 8000 * 2  # 8000 samples × 2 bytes (int16), ~0.5s
    audio_queue: queue.Queue[bytes] = queue.Queue()

    def _feed_proc(proc, stop_evt):
        """Background thread: reads filter_proc stdout into audio_queue."""
        while not stop_evt.is_set():
            try:
                data = proc.stdout.read(CHUNK)
                if not data:
                    break
                audio_queue.put(data)
            except Exception:
                break

    def _publish_transcript(text: str) -> None:
        """Apply LLM correction if enabled, then publish to MQTT."""
        if LLM_CORRECT:
            text = _llm_correct(text)
        rc = mqttc.publish(TOPIC_TRANSCRIPT,
                           json.dumps({"text": text, "ts": time.time()}))
        if rc.rc != mqtt.MQTT_ERR_SUCCESS:
            log(f"WARNING: transcript publish failed (rc={rc.rc}) — may be lost: {text!r}")

    last_partial = ""
    try:
        while True:
            device_change.clear()
            tts_pause.clear()
            tts_resume.clear()
            _wait_for_audio(_current_device[0])

            capture_proc = filter_proc = reader_thread = None
            reader_stop = threading.Event()
            vad_state = _make_vad_state()

            try:
                capture_proc, filter_proc = _open_capture(_current_device[0])
                reader_thread = threading.Thread(
                    target=_feed_proc, args=(filter_proc, reader_stop), daemon=True
                )
                reader_thread.start()

                mqttc.publish(TOPIC_DEVICE_STATUS,
                              json.dumps({"device": _current_device[0], "status": "ok"}),
                              retain=True)
                log("Listening. Speak a query...")
                last_partial = ""

                while not device_change.is_set() and not tts_pause.is_set():
                    if filter_proc.poll() is not None:
                        log("audio filter process exited unexpectedly")
                        break
                    try:
                        data = audio_queue.get(timeout=0.5)
                    except queue.Empty:
                        continue

                    # ── VAD: auto-finalize on silence ──────────────────────
                    if not muted.is_set():
                        if _vad_process(vad, data, vad_state):
                            force_finalize.set()

                    # ── force-finalize (manual or VAD-triggered) ───────────
                    if force_finalize.is_set():
                        is_manual = manual_finalize.is_set()
                        force_finalize.clear()
                        manual_finalize.clear()
                        text = stt.final_result() or last_partial
                        if not text:
                            if is_manual:
                                text = _next_fallback()
                                log(f"Force-finalize: no speech — using fallback: {text!r}")
                            else:
                                log("VAD silence: no speech detected, skipping publish.")
                                stt.recognizer = KaldiRecognizer(stt.model, SAMPLE_RATE)
                                reset_recognizer.clear()
                                continue
                        log(f"Force-final: {text!r}")
                        _publish_transcript(text)
                        last_partial = ""
                        mqttc.publish(TOPIC_PARTIAL, json.dumps({"text": ""}))
                        stt.recognizer = KaldiRecognizer(stt.model, SAMPLE_RATE)
                        reset_recognizer.clear()
                        log("Recognizer reset (force-finalize).")
                        continue

                    # ── recognizer reset (post-TTS) ────────────────────────
                    if reset_recognizer.is_set():
                        reset_recognizer.clear()
                        last_partial = ""
                        vad_state = _make_vad_state()
                        mqttc.publish(TOPIC_PARTIAL, json.dumps({"text": ""}))
                        stt.recognizer = KaldiRecognizer(stt.model, SAMPLE_RATE)
                        log("Recognizer reset.")

                    if muted.is_set():
                        continue

                    result = stt.accept_audio(data)

                    if result["type"] == "final" and result["text"]:
                        text = result["text"]
                        conf = result.get("confidence")

                        # ── confidence filter ──────────────────────────────
                        if conf is not None and conf < CONFIDENCE_THRESHOLD:
                            log(f"Low confidence ({conf:.2f} < {CONFIDENCE_THRESHOLD}) — skipping: {text!r}")
                            last_partial = ""
                            continue

                        last_partial = ""
                        log(f"Final{f' (conf={conf:.2f})' if conf is not None else ''}: {text!r}")
                        _publish_transcript(text)

                    elif result["type"] == "partial" and result["text"]:
                        last_partial = result["text"]
                        mqttc.publish(TOPIC_PARTIAL, json.dumps({"text": result["text"]}))
                        print(f"\rPartial: {result['text']}", end="", flush=True)

            except Exception as e:
                log(f"Stream error: {e}")
                if not device_change.is_set():
                    time.sleep(2)
            finally:
                reader_stop.set()
                _close_capture(capture_proc, filter_proc)
                if reader_thread is not None:
                    reader_thread.join(timeout=2)

            if tts_pause.is_set() and not device_change.is_set():
                log("Pausing capture (TTS speaking on ALSA device)")
                tts_resume.wait()
                log("Resuming capture")
            if device_change.is_set():
                _current_device[0] = next_device[0]
                last_partial = ""
                log(f"Switching to device {_current_device[0]!r}")

    except KeyboardInterrupt:
        log("Interrupted.")
    finally:
        mqttc.loop_stop()
        mqttc.disconnect()
        log("Done.")


if __name__ == "__main__":
    main()

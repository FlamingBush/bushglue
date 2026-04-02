#!/usr/bin/env python3
"""
Speech-to-Text MQTT publisher.
Reads from microphone using Vosk, publishes final transcriptions to
bush/pipeline/stt/transcript as {"text": "...", "ts": <epoch>}.

Mutes itself while TTS is speaking (bush/pipeline/tts/speaking) and
unmutes on bush/pipeline/tts/done, resetting the Vosk recognizer so
any partial state from hearing TTS speech is discarded.

Accepts runtime device changes via bush/audio/stt/set-device {"device": <int|str>}.
"""
import json
import os
import pathlib
import queue
import subprocess as _subprocess
import sys
import threading
import time

import paho.mqtt.client as mqtt

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


def _is_alsa_device(device) -> bool:
    """Return True if device is an ALSA hw: specifier rather than a PA source name."""
    s = str(device)
    return s.startswith("hw:") or s.startswith("plughw:")


def _pa_source_present(device) -> bool:
    """Check if a PulseAudio source is available via pactl."""
    try:
        result = _subprocess.run(
            ["pactl", "list", "short", "sources"],
            capture_output=True, text=True, timeout=5,
        )
        return str(device) in result.stdout
    except Exception:
        return False


def _alsa_device_present(device) -> bool:
    """Check if an ALSA capture device is present via /proc/asound."""
    s = str(device)
    card = s.split(":")[1].split(",")[0] if ":" in s else s
    path = f"/proc/asound/card{card}" if card.isdigit() else f"/proc/asound/{card}"
    return pathlib.Path(path).exists()


def _wait_for_audio(device) -> None:
    """Block until the audio source appears, logging each retry."""
    if _is_alsa_device(device):
        check = lambda: _alsa_device_present(device)
    else:
        check = lambda: _pa_source_present(device)
    while not check():
        log(f"Audio source {device!r} not yet available — retrying in {_AUDIO_RETRY_INTERVAL}s...")
        time.sleep(_AUDIO_RETRY_INTERVAL)


def main():
    broker = get_mqtt_broker()
    log(f"MQTT broker: {broker}:{MQTT_PORT}")

    # ── startup validation ─────────────────────────────────────────────────
    log(f"STT_DIR:    {STT_DIR}")
    log(f"MODEL_PATH: {MODEL_PATH}")
    log(f"STT_DEVICE: {STT_DEVICE!r}")

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
    _mute_timer: list[threading.Timer | None] = [None]

    # ── device change ──────────────────────────────────────────────────────
    device_change = threading.Event()
    next_device = [STT_DEVICE]
    _current_device = [STT_DEVICE]  # list wrapper for cross-thread read safety

    # ── ALSA TTS pause/resume (take turns on hw: devices) ──────────────────
    # When TTS speaks on an ALSA device, STT releases the capture interface
    # so the OHCI controller doesn't get concurrent playback+capture opens.
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
    mqttc = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)

    def on_message(client, userdata, msg):
        if msg.topic == TOPIC_TTS_SPEAKING:
            on_tts_speaking()
        elif msg.topic == TOPIC_TTS_DONE:
            on_tts_done()
        elif msg.topic == TOPIC_FORCE_FINALIZE:
            log("Force-finalize requested.")
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
        # QoS 1 on mute/unmute signals — loss causes feedback loop or permanent mute
        # TODO: for full reliability across reconnects, use stable client ID + clean_session=False
        client.subscribe(TOPIC_TTS_SPEAKING, qos=1)
        client.subscribe(TOPIC_TTS_DONE, qos=1)
        client.subscribe(TOPIC_SET_DEVICE)
        client.subscribe(TOPIC_FORCE_FINALIZE)
        client.subscribe(TOPIC_PIPELINE_PING)
        # Publish current device on reconnect
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
    audio_queue: queue.Queue[bytes] = queue.Queue()

    # ── restartable audio loop ─────────────────────────────────────────────
    CHUNK = 8000 * 2  # 8000 samples × 2 bytes (int16)

    def _feed_parec(proc, stop_evt):
        """Background thread: reads parec stdout into audio_queue."""
        while not stop_evt.is_set():
            try:
                data = proc.stdout.read(CHUNK)
                if not data:
                    break
                audio_queue.put(data)
            except Exception:
                break

    last_partial = ""  # preserved across device sessions; cleared on device change
    try:
        while True:
            device_change.clear()
            tts_pause.clear()
            tts_resume.clear()
            _wait_for_audio(_current_device[0])

            parec_proc = None
            reader_stop = threading.Event()
            reader_thread = None
            try:
                if _is_alsa_device(_current_device[0]):
                    log(f"Opening ALSA device {_current_device[0]!r} at {SAMPLE_RATE} Hz...")
                    parec_proc = _subprocess.Popen(
                        ["arecord", "-D", str(_current_device[0]),
                         "-f", "S16_LE", "-c", "1", f"-r{SAMPLE_RATE}", "-t", "raw"],
                        stdout=_subprocess.PIPE,
                        stderr=_subprocess.DEVNULL,
                    )
                else:
                    log(f"Opening PA source {_current_device[0]!r} at {SAMPLE_RATE} Hz...")
                    parec_proc = _subprocess.Popen(
                        ["parec", "--device", str(_current_device[0]),
                         "--format=s16le", f"--rate={SAMPLE_RATE}", "--channels=1"],
                        stdout=_subprocess.PIPE,
                        stderr=_subprocess.DEVNULL,
                    )
                reader_thread = threading.Thread(
                    target=_feed_parec, args=(parec_proc, reader_stop), daemon=True
                )
                reader_thread.start()

                mqttc.publish(TOPIC_DEVICE_STATUS,
                              json.dumps({"device": _current_device[0], "status": "ok"}),
                              retain=True)
                log("Listening. Speak a query...")
                last_partial = ""
                while not device_change.is_set() and not tts_pause.is_set():
                    if parec_proc.poll() is not None:
                        log("parec exited unexpectedly")
                        break
                    try:
                        data = audio_queue.get(timeout=0.5)
                    except queue.Empty:
                        continue

                    if force_finalize.is_set():
                        force_finalize.clear()
                        text = stt.final_result() or last_partial
                        if not text:
                            log("Force-finalize: no speech detected, skipping publish.")
                            stt.recognizer = KaldiRecognizer(stt.model, SAMPLE_RATE)
                            reset_recognizer.clear()  # prevent duplicate reset this iteration
                            log("Recognizer reset (force-finalize, no speech).")
                            continue
                        log(f"Force-final: {text!r}")
                        rc = mqttc.publish(TOPIC_TRANSCRIPT,
                                           json.dumps({"text": text, "ts": time.time()}))
                        if rc.rc != mqtt.MQTT_ERR_SUCCESS:
                            log(f"WARNING: transcript publish failed (rc={rc.rc}) — message may be lost: {text!r}")
                        last_partial = ""
                        mqttc.publish(TOPIC_PARTIAL, json.dumps({"text": ""}))
                        stt.recognizer = KaldiRecognizer(stt.model, SAMPLE_RATE)
                        reset_recognizer.clear()  # prevent duplicate reset this iteration
                        log("Recognizer reset (force-finalize).")
                        continue

                    if reset_recognizer.is_set():
                        reset_recognizer.clear()
                        last_partial = ""
                        mqttc.publish(TOPIC_PARTIAL, json.dumps({"text": ""}))
                        stt.recognizer = KaldiRecognizer(stt.model, SAMPLE_RATE)
                        log("Recognizer reset.")

                    if muted.is_set():
                        continue

                    result = stt.accept_audio(data)

                    if result["type"] == "final" and result["text"]:
                        text = result["text"]
                        last_partial = ""
                        log(f"Final: {text!r}")
                        rc = mqttc.publish(TOPIC_TRANSCRIPT,
                                           json.dumps({"text": text, "ts": time.time()}))
                        if rc.rc != mqtt.MQTT_ERR_SUCCESS:
                            log(f"WARNING: transcript publish failed (rc={rc.rc}) — message may be lost: {text!r}")
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
                if parec_proc is not None:
                    try:
                        parec_proc.kill()
                        parec_proc.wait()
                    except Exception:
                        pass
                if reader_thread is not None:
                    reader_thread.join(timeout=2)

            if tts_pause.is_set() and not device_change.is_set():
                log("Pausing capture (TTS speaking on ALSA device)")
                tts_resume.wait()
                log("Resuming capture")
            if device_change.is_set():
                _current_device[0] = next_device[0]
                last_partial = ""  # discard stale partial from previous device session
                log(f"Switching to device {_current_device[0]!r}")

    except KeyboardInterrupt:
        log("Interrupted.")
    finally:
        mqttc.loop_stop()
        mqttc.disconnect()
        log("Done.")


if __name__ == "__main__":
    main()

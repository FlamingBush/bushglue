#!/usr/bin/env python3
"""
Speech-to-Text MQTT publisher.
Reads from microphone using Vosk, publishes final transcriptions to
bush/pipeline/stt/transcript as {"text": "...", "ts": <epoch>}.

Mutes itself while TTS is speaking (bush/pipeline/tts/speaking) and
unmutes on bush/pipeline/tts/done, resetting the Vosk recognizer so
any partial state from hearing TTS speech is discarded.

Accepts runtime device changes via bush/audio/stt/set-device {"device": <int|str>}.

Two pipelines:
  - LEGACY (default, STT_USE_VAD=0): byte-identical to the original Vosk
    streaming path. parec/arecord 16k -> SpeechToText.accept_audio() -> MQTT.
  - NEW (STT_USE_VAD=1): VAD-endpointed utterance-level pipeline. Optionally
    captures at 48k with RNNoise + soxr 48->16, then VAD endpoints into
    complete utterances passed to a pluggable STTEngine adapter.
"""
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

from bushutil import get_mqtt_broker, load_audio_device, save_audio_device

# ── paths / device ─────────────────────────────────────────────────────────
_REPO_ROOT = pathlib.Path(__file__).resolve().parents[4]  # bushglue/
MODEL_PATH = os.environ.get("STT_MODEL", str(_REPO_ROOT / "data" / "vosk-model"))
_dev = os.environ.get("STT_DEVICE")
if _dev:
    STT_DEVICE = int(_dev) if _dev.isdigit() else _dev  # int index or string name
else:
    STT_DEVICE = load_audio_device("stt")  # restore last saved device (or None)
SAMPLE_RATE = 16000  # legacy path uses this; SpeechToText still imports it

# ── pipeline flags ─────────────────────────────────────────────────────────
# Default-off: STT_USE_VAD=0 keeps byte-identical legacy behavior.
STT_USE_VAD = os.environ.get("STT_USE_VAD", "0") not in ("0", "false", "False", "")
STT_USE_RNNOISE = os.environ.get("STT_USE_RNNOISE", "0") not in ("0", "false", "False", "")
STT_ENGINE_NAME = os.environ.get("STT_ENGINE", "vosk").lower()
# Drop transcripts whose mean word-level confidence falls below this floor.
# Default 0.6 matches the threshold used in the prior STT-accuracy work
# (middog/bushglue commit ff29f2c, Apr 2026).
STT_MIN_CONFIDENCE = float(os.environ.get("STT_MIN_CONFIDENCE", "0.6"))

# Capture rate flips when RNNoise is enabled (48k native frames).
# Legacy path always uses 16k.
CAPTURE_SAMPLE_RATE = 48000 if (STT_USE_VAD and STT_USE_RNNOISE) else 16000

# Recognizer rate is always 16 kHz (Vosk model + Whisper + Silero VAD).
RECOGNIZER_SAMPLE_RATE = 16000

# Chunk size matches 0.5s of audio at the active capture rate, int16 LE.
CHUNK = int(CAPTURE_SAMPLE_RATE * 0.5) * 2

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
TOPIC_PARTIAL         = "bush/pipeline/stt/partial"  # legacy-only; new path doesn't publish
TOPIC_TTS_SPEAKING    = "bush/pipeline/tts/speaking"
TOPIC_TTS_DONE        = "bush/pipeline/tts/done"
TOPIC_SET_DEVICE      = "bush/audio/stt/set-device"
TOPIC_DEVICE_STATUS   = "bush/audio/stt/device"
TOPIC_FORCE_FINALIZE  = "bush/pipeline/stt/force-finalize"
TOPIC_PIPELINE_PING   = "bush/pipeline/ping"
TOPIC_PIPELINE_PONG   = "bush/pipeline/pong"
TOPIC_TTS_DEVICE      = "bush/audio/tts/device"
MQTT_PORT = 1883


def log(msg: str):
    print(f"[stt-service] {msg}", flush=True)


_AUDIO_RETRY_INTERVAL = 10  # seconds between device-ready checks


def _is_alsa_device(device) -> bool:
    """Return True if device is an ALSA hw: specifier rather than a PA source name."""
    s = str(device)
    return s.startswith("hw:") or s.startswith("plughw:")


def _alsa_card(device) -> str | None:
    """Extract card name from an ALSA device string, e.g. 'hw:Microphone' -> 'Microphone'."""
    s = str(device)
    for prefix in ("plughw:", "hw:"):
        if s.startswith(prefix):
            return s[len(prefix):].split(",")[0]
    return None


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


def _wait_for_audio(device, interrupt: threading.Event | None = None) -> bool:
    """Block until the audio source appears. Returns False if interrupted by a device change."""
    if _is_alsa_device(device):
        check = lambda: _alsa_device_present(device)
    else:
        check = lambda: _pa_source_present(device)
    while not check():
        log(f"Audio source {device!r} not yet available — retrying in {_AUDIO_RETRY_INTERVAL}s...")
        if interrupt and interrupt.wait(_AUDIO_RETRY_INTERVAL):
            return False
        elif not interrupt:
            time.sleep(_AUDIO_RETRY_INTERVAL)
    return True


# ── factories for the new pipeline ─────────────────────────────────────────

def _build_engine():
    """Construct the STT engine selected by STT_ENGINE.

    Only called on the new VAD path. Raises RuntimeError if the engine name
    is unknown; a missing dependency surfaces as ImportError to fail loud at
    startup (no silent fallback to vosk).
    """
    if STT_ENGINE_NAME == "vosk":
        from bush_stt.engines.vosk import VoskEngine
        log(f"using engine: vosk (model={MODEL_PATH})")
        return VoskEngine(model_path=MODEL_PATH)
    elif STT_ENGINE_NAME == "whisper-bindings":
        from bush_stt.engines.whisper_bindings import WhisperBindingsEngine
        log("using engine: whisper-bindings")
        return WhisperBindingsEngine()
    elif STT_ENGINE_NAME == "whisper-subprocess":
        from bush_stt.engines.whisper_subprocess import WhisperSubprocessEngine
        log("using engine: whisper-subprocess")
        return WhisperSubprocessEngine()
    elif STT_ENGINE_NAME == "whisper-rknn":
        from bush_stt.engines.whisper_rknn import WhisperRknnEngine
        log("using engine: whisper-rknn")
        return WhisperRknnEngine()
    else:
        raise RuntimeError(
            f"Unknown STT_ENGINE={STT_ENGINE_NAME!r}; "
            f"must be vosk|whisper-bindings|whisper-subprocess|whisper-rknn"
        )


def _build_pipeline():
    """Build VAD pipeline components.

    Returns (vad, denoise_or_none, resampler_or_none).
    """
    from bush_stt.vad import VadEndpointer
    vad = VadEndpointer()
    if STT_USE_RNNOISE:
        from bush_stt.denoise import RnnoiseFilter
        denoise = RnnoiseFilter()
        # Stateful streaming resampler (48k -> 16k); arbitrary input length OK.
        import soxr
        resampler = soxr.ResampleStream(48000, 16000, 1, dtype="int16")
        return vad, denoise, resampler
    return vad, None, None


def main():
    broker = get_mqtt_broker()
    log(f"MQTT broker: {broker}:{MQTT_PORT}")
    log(
        f"flags: STT_USE_VAD={STT_USE_VAD} STT_USE_RNNOISE={STT_USE_RNNOISE} "
        f"STT_ENGINE={STT_ENGINE_NAME} CAPTURE_SAMPLE_RATE={CAPTURE_SAMPLE_RATE}"
    )

    # ── mute gate ──────────────────────────────────────────────────────────
    MUTE_TIMEOUT_S = 30
    muted = threading.Event()
    reset_recognizer = threading.Event()
    force_finalize = threading.Event()
    _mute_timer: list[threading.Timer | None] = [None]

    # ── device change ──────────────────────────────────────────────────────
    device_change = threading.Event()
    next_device = [STT_DEVICE]   # list so inner functions can mutate it

    # ── ALSA TTS pause/resume (take turns on shared hw: devices) ────────────
    tts_pause  = threading.Event()
    tts_resume = threading.Event()
    tts_device = [None]  # tracked via MQTT retained message

    # ── VAD object slot (set on new-pipeline branch; legacy path leaves None) ─
    # Used by on_tts_speaking / on_tts_done to coordinate with VAD state.
    vad_ref: list = [None]

    def on_tts_done():
        if _mute_timer[0] is not None:
            _mute_timer[0].cancel()
            _mute_timer[0] = None
        muted.clear()
        reset_recognizer.set()
        tts_pause.clear()
        tts_resume.set()
        if vad_ref[0] is not None:
            try:
                vad_ref[0].reset()
            except Exception as e:
                log(f"vad.reset() error: {e}")
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
        if vad_ref[0] is not None:
            try:
                vad_ref[0].drop_in_flight()
            except Exception as e:
                log(f"vad.drop_in_flight() error: {e}")
        stt_card = _alsa_card(current_device)
        tts_card = _alsa_card(tts_device[0]) if tts_device[0] else None
        if stt_card and stt_card == tts_card:
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
        elif msg.topic == TOPIC_TTS_DEVICE:
            try:
                data = json.loads(msg.payload)
                tts_device[0] = data.get("device")
            except Exception:
                pass
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
        client.subscribe(TOPIC_TTS_SPEAKING)
        client.subscribe(TOPIC_TTS_DONE)
        client.subscribe(TOPIC_SET_DEVICE)
        client.subscribe(TOPIC_FORCE_FINALIZE)
        client.subscribe(TOPIC_PIPELINE_PING)
        client.subscribe(TOPIC_TTS_DEVICE)
        # Publish current device on reconnect
        client.publish(TOPIC_DEVICE_STATUS,
                       json.dumps({"device": next_device[0]}), retain=True)

    mqttc.on_connect = on_connect
    mqttc.on_message = on_message
    mqttc.connect(broker, MQTT_PORT, 60)
    mqttc.loop_start()
    log("MQTT connected.")

    # ── shared capture state ───────────────────────────────────────────────
    audio_queue: queue.Queue[bytes] = queue.Queue()
    current_device = STT_DEVICE

    def _feed_capture(proc, stop_evt):
        """Background thread: reads parec/arecord stdout into audio_queue."""
        while not stop_evt.is_set():
            try:
                data = proc.stdout.read(CHUNK)
                if not data:
                    break
                audio_queue.put(data)
            except Exception:
                break

    def _open_capture(device, sample_rate):
        """Spawn parec or arecord at the given rate. Returns Popen object."""
        if _is_alsa_device(device):
            # plughw: lets ALSA handle resampling/channel conversion (e.g. Yeti)
            alsa_dev = str(device)
            if alsa_dev.startswith("hw:"):
                alsa_dev = "plug" + alsa_dev
            log(f"Opening ALSA device {alsa_dev!r} at {sample_rate} Hz...")
            return _subprocess.Popen(
                ["arecord", "-D", alsa_dev,
                 "-f", "S16_LE", "-c", "1", f"-r{sample_rate}", "-t", "raw"],
                stdout=_subprocess.PIPE,
                stderr=_subprocess.DEVNULL,
            )
        else:
            log(f"Opening PA source {device!r} at {sample_rate} Hz...")
            return _subprocess.Popen(
                ["parec", "--device", str(device),
                 "--format=s16le", f"--rate={sample_rate}", "--channels=1"],
                stdout=_subprocess.PIPE,
                stderr=_subprocess.DEVNULL,
            )

    # ── pipeline-specific pre-flight ──────────────────────────────────────
    if STT_USE_VAD:
        log("starting NEW pipeline (VAD enabled)")
        engine = _build_engine()
        vad, denoise, resampler = _build_pipeline()
        vad_ref[0] = vad
    else:
        log("starting LEGACY pipeline (Vosk streaming)")
        # Warn loudly if non-default knobs were set on the legacy path so
        # ops doesn't think they took effect.
        if os.environ.get("STT_USE_RNNOISE", "0") not in ("0", "false", "False", ""):
            log("WARN: STT_USE_RNNOISE has no effect when STT_USE_VAD=0 "
                "(legacy path uses 16k Vosk streaming)")
        if os.environ.get("STT_ENGINE", "vosk").lower() != "vosk":
            log("WARN: STT_ENGINE has no effect when STT_USE_VAD=0 "
                "(legacy path uses streaming Vosk)")
        # Legacy SpeechToText (streaming Vosk) and KaldiRecognizer for resets
        from bush_stt.transcriber import SpeechToText
        from vosk import KaldiRecognizer
        stt = SpeechToText(model_path=MODEL_PATH, sample_rate=SAMPLE_RATE)
        engine = None
        vad = None
        denoise = None
        resampler = None

    # ── helpers per-iteration ─────────────────────────────────────────────

    def _run_legacy_iteration():
        """Inner capture loop body for legacy streaming Vosk path.

        Behavior must remain byte-identical to the original implementation.
        """
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
                text = stt.final_result() or last_partial or _next_fallback()
                log(f"Force-final: {text!r}")
                mqttc.publish(TOPIC_TRANSCRIPT,
                              json.dumps({"text": text, "ts": time.time()}))
                last_partial = ""
                mqttc.publish(TOPIC_PARTIAL, json.dumps({"text": ""}))
                stt.recognizer = KaldiRecognizer(stt.model, SAMPLE_RATE)
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
                mqttc.publish(TOPIC_TRANSCRIPT,
                              json.dumps({"text": text, "ts": time.time()}))
            elif result["type"] == "partial" and result["text"]:
                last_partial = result["text"]
                mqttc.publish(TOPIC_PARTIAL, json.dumps({"text": result["text"]}))
                print(f"\rPartial: {result['text']}", end="", flush=True)

    def _run_new_iteration():
        """Inner capture loop body for the new VAD + engine pipeline.

        Captures at CAPTURE_SAMPLE_RATE; optionally denoises (48k native) and
        resamples 48->16; feeds 16k PCM to the VAD endpointer, calls
        engine.transcribe() on each emitted utterance and publishes results.
        """
        # Lazy numpy import only on the new path (avoid cost on legacy startup)
        import numpy as np
        while not device_change.is_set() and not tts_pause.is_set():
            if parec_proc.poll() is not None:
                log("capture process exited unexpectedly")
                break
            try:
                data = audio_queue.get(timeout=0.5)
            except queue.Empty:
                continue

            if force_finalize.is_set():
                force_finalize.clear()
                # New semantics (v1): emit a canned phrase, drop in-flight VAD
                # state. We don't try to harvest a half-buffered utterance —
                # keep it simple and predictable.
                text = _next_fallback()
                log(f"Force-final (canned): {text!r}")
                mqttc.publish(TOPIC_TRANSCRIPT,
                              json.dumps({"text": text, "ts": time.time()}))
                vad.drop_in_flight()
                if denoise is not None:
                    denoise.reset()
                continue

            if reset_recognizer.is_set():
                reset_recognizer.clear()
                vad.reset()
                if denoise is not None:
                    denoise.reset()
                log("Recognizer/VAD reset.")

            if muted.is_set():
                continue

            # Stage 1: optional RNNoise (operates on 48k native frames)
            if denoise is not None:
                data = denoise.process(data)
                if not data:
                    # Less than one 480-sample frame buffered; wait for more
                    continue
                # Stage 2: 48 -> 16 resample
                arr_in = np.frombuffer(data, dtype=np.int16)
                arr_out = resampler.resample_chunk(arr_in)
                if arr_out.size == 0:
                    continue
                pcm_16k = arr_out.tobytes()
            else:
                # No denoiser: capture is already 16k mono int16 LE
                pcm_16k = data

            # Stage 3: VAD endpoint -> zero or more complete utterances
            utterances = vad.feed(pcm_16k)
            for utt in utterances:
                ms = (len(utt) // 2) * 1000 // RECOGNIZER_SAMPLE_RATE
                log(f"VAD emitted utterance: {len(utt)} bytes ({ms} ms)")
                try:
                    result = engine.transcribe(utt)
                    text = (result.get("text") or "").strip()
                    conf = float(result.get("confidence", 0.0))
                    if not text:
                        log("Engine returned empty text; not publishing")
                        continue
                    if conf < STT_MIN_CONFIDENCE:
                        log(f"Dropping low-confidence transcript "
                            f"({conf:.2f} < {STT_MIN_CONFIDENCE:.2f}): {text!r}")
                        continue
                    log(f"Final: {text!r} (conf={conf:.2f})")
                    mqttc.publish(
                        TOPIC_TRANSCRIPT,
                        json.dumps({
                            "text": text,
                            "confidence": conf,
                            "ts": time.time(),
                        }),
                    )
                except Exception as e:
                    log(f"engine.transcribe error: {e}")

    # ── main capture loop (shared across both pipelines) ──────────────────
    try:
        while True:
            device_change.clear()
            tts_pause.clear()
            tts_resume.clear()
            if not _wait_for_audio(current_device, interrupt=device_change):
                current_device = next_device[0]
                log(f"Device changed to {current_device!r} while waiting")
                continue

            parec_proc = None
            reader_stop = threading.Event()
            reader_thread = None
            try:
                parec_proc = _open_capture(current_device, CAPTURE_SAMPLE_RATE)
                reader_thread = threading.Thread(
                    target=_feed_capture, args=(parec_proc, reader_stop), daemon=True
                )
                reader_thread.start()

                mqttc.publish(TOPIC_DEVICE_STATUS,
                              json.dumps({"device": current_device, "status": "ok"}),
                              retain=True)
                log("Listening. Speak a query...")

                if STT_USE_VAD:
                    _run_new_iteration()
                else:
                    _run_legacy_iteration()

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
                current_device = next_device[0]
                log(f"Switching to device {current_device!r}")

    except KeyboardInterrupt:
        log("Interrupted.")
    finally:
        # New-pipeline resource cleanup (best-effort — never raise during shutdown)
        if STT_USE_VAD:
            if vad is not None:
                try: vad.close()
                except Exception: pass
            if denoise is not None:
                try: denoise.close()
                except Exception: pass
            if engine is not None:
                try: engine.close()
                except Exception: pass
        mqttc.loop_stop()
        mqttc.disconnect()
        log("Done.")


if __name__ == "__main__":
    main()

"""`bush-cue play` -- play a cue sheet on the odroid.

Owns the audio clock: plays the track and drives the valve + flame off the playhead.
The valve waveform is streamed AHEAD of the playhead as binary frames (MQTT
bush/fire/valve/stream -> bush_valve_ble bridge -> BLE); the firmware buffers them
and executes on its own clock, so BLE jitter doesn't move motion timing. Flame
pulses go to bush/flame/pulse with a latency-lead so they land on-beat.

Suspends the sentiment driver via bush/fire/valve/auto off (retained) and restores
it on exit (incl. SIGTERM/crash) so the valve is never left orphaned. All flame
limits are re-asserted here -- the firmware enforces none.
"""
from __future__ import annotations

import argparse
import json
import signal
import time

from . import cuesheet, features, safety, wire
from .features import log

TOPIC_STREAM = "bush/fire/valve/stream"
TOPIC_FLAME = "bush/flame/pulse"
TOPIC_AUTO = "bush/fire/valve/auto"
TOPIC_PONG = "bush/fire/valve/pong"
MQTT_PORT = 1883

LOOKAHEAD_S = 1.2     # keep this much of the waveform buffered ahead of the playhead
BATCH_S = 0.4         # samples per stream frame
STALE_S = 0.5         # drop a flame cue if we're already this far past it


def _summarize(sheet: dict, no_flame: bool) -> None:
    v = sheet["valve"]
    flame = [] if no_flame else safety.filter_flame(
        sheet.get("flame", []), float(sheet.get("knobs", {}).get("max_cue_rate", 6.0)))
    log(f"sheet: {sheet['duration_s']}s, preset={sheet.get('preset')}, {sheet['tempo_bpm']} BPM")
    log(f"valve: {len(v['pos'])} samples @ {v['rate_hz']} Hz "
        f"(range {min(v['pos']):.3f}..{max(v['pos']):.3f})")
    log(f"flame: {len(flame)} pulses after safety")
    for c in flame[:20]:
        print(f"  {c['t']:7.3f}s  {c['valve']:6}  {c['ms']}ms")
    if len(flame) > 20:
        print(f"  ... +{len(flame) - 20} more")


def _measure_latency(mqttc, pong: dict, n: int = 5) -> float:
    """One-way MQTT->bridge->BLE->firmware latency (s), via ping/pong round-trips."""
    rtts = []
    for tok in range(1, n + 1):
        sent = time.monotonic()
        mqttc.publish(TOPIC_STREAM, wire.ping(tok), qos=1)
        deadline = sent + 0.3
        while time.monotonic() < deadline:
            if tok in pong:
                rtts.append(pong[tok] - sent)
                break
            time.sleep(0.005)
    if not rtts:
        log("no pong from valve node -- using 50ms default offset (sync uncalibrated)")
        return 0.05
    rtts.sort()
    one_way = rtts[len(rtts) // 2] / 2.0
    log(f"valve link one-way latency ~{one_way * 1000:.0f}ms")
    return one_way


def _play(sheet: dict, args: argparse.Namespace) -> int:
    import paho.mqtt.client as mqtt
    from bushutil import get_mqtt_broker

    rate = int(sheet["valve"]["rate_hz"])
    samples = wire.quantize(sheet["valve"]["pos"])
    nsamp = len(samples)
    end_s = nsamp / rate
    flame = [] if args.no_flame else safety.filter_flame(
        sheet.get("flame", []), float(sheet.get("knobs", {}).get("max_cue_rate", 6.0)))
    lead = args.latency_lead_ms / 1000.0

    audio = features.decode_to_mono(args.audio) if args.audio else None

    pong: dict[int, float] = {}
    mqttc = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)

    def on_msg(_c, _u, m):
        if m.topic == TOPIC_PONG:
            try:
                tok = int(m.payload.decode().split()[0])
                pong[tok] = time.monotonic()
            except Exception:
                pass

    mqttc.on_message = on_msg
    mqttc.connect(get_mqtt_broker(), MQTT_PORT, 60)
    mqttc.loop_start()
    mqttc.subscribe(TOPIC_PONG)
    mqttc.publish(TOPIC_AUTO, "off", qos=1, retain=True)  # suspend sentiment driver

    stop = {"v": False}
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *a: stop.__setitem__("v", True))

    try:
        one_way = _measure_latency(mqttc, pong)
        if audio is not None:
            import sounddevice as sd
            sd.play(audio, samplerate=features.SR)
        t0 = time.monotonic()
        mqttc.publish(TOPIC_STREAM, wire.start(rate, int(one_way * 1000)), qos=1)
        log(f"playing: {end_s:.1f}s, {nsamp} valve samples, {len(flame)} flame cues")

        sent = 0   # next valve sample index to stream
        fi = 0     # next flame cue
        batch = max(1, int(BATCH_S * rate))
        while not stop["v"]:
            ph = time.monotonic() - t0
            if ph > end_s + 0.5:
                break
            want = min(nsamp, int((ph + LOOKAHEAD_S) * rate))
            while sent < want:
                n = min(batch, want - sent)
                mqttc.publish(TOPIC_STREAM, wire.samples(sent, samples[sent:sent + n]), qos=1)
                sent += n
            while fi < len(flame) and flame[fi]["t"] - lead <= ph:
                c = flame[fi]
                fi += 1
                if c["t"] - lead >= ph - STALE_S:
                    mqttc.publish(TOPIC_FLAME,
                                  json.dumps({"valve": c["valve"], "ms": c["ms"]}), qos=0)
            time.sleep(0.02)
    finally:
        mqttc.publish(TOPIC_STREAM, wire.stop(), qos=1)
        if audio is not None:
            import sounddevice as sd
            sd.stop()
        mqttc.publish(TOPIC_AUTO, "on", qos=1, retain=True)  # restore sentiment driver
        time.sleep(0.2)
        mqttc.loop_stop()
        mqttc.disconnect()
    log("done")
    return 0


def run(args: argparse.Namespace) -> int:
    sheet = cuesheet.read(args.sheet)
    if args.dry_run:
        _summarize(sheet, args.no_flame)
        return 0
    return _play(sheet, args)

# STT degradation report — 2026-05-07

`bush-stt.service` ran for 12 days (since Apr 25 19:16 UTC). On May 7 ~19:54 UTC,
when a user joined the Discord VC and started talking, Vosk emitted only single
short noise tokens (`'huh'`, `'hum'`, `'him'`, `'when'`, `'hitler'`) at a regular
~20 s cadence. A `systemctl restart bush-stt` at 20:27:53 UTC fully fixed it
with no other changes — same audio source, same model files, same downstream
config. Filing this for whoever owns STT reliability.

## Failure profile

Failure window: 2026-05-07 19:54 → 20:28 UTC (34 min).

- 63 `Final:` outputs in the failure window. **Every one** was a single short
  token. Distribution: `huh ×59, when ×1, hum ×1, hitler ×1, him ×1`.
- Cadence was very regular (~20 s between finals) regardless of speech activity.
  The `'huh'` runs were not aligned with breath/speech pauses.
- After restart, the same speakers on the same audio path immediately produced
  multi-word, recognizable transcripts:
  - `'oh wish'`
  - `"can't make me well"`
  - `'hello bush'`
  - `"don't make me bro"`
  - `'what is a meeting on fire'` (this is the integration test phrase)

## Evidence the input audio was fine

The "audio reaching Vosk is silence" hypothesis is ruled out:

- A 15 s `parec --device loopback-inject.monitor --format=s16le --rate=16000 --channels=1`
  capture taken **during** the failure window, while the user was speaking,
  produced 13.43 s of audio with `peak=12896` (≈ −8 dBFS) and clear speech
  segments in 6 of 26 500 ms windows. Levels were healthy.
- `pactl list short sinks` showed `loopback-inject … RUNNING` with sink-inputs
  from the discord bot's pacat producer.
- After restart, the new `bush-stt` process inherited the **same** PA source
  (`loopback-inject.monitor`) with no routing/device change and recognized
  speech immediately.

## Process state at time of failure

- PID 12480, ELAPSED `12-01:09:34`, RSS 462 MB, %CPU 0.0 (sleeping at sample),
  cumulative CPU `12h 34min 13.182s` over 12 days (~4.4 % avg).
- One child: `parec --device loopback-inject.monitor --format=s16le --rate=16000 --channels=1`
  PID 13612, also 12 days old.
- MQTT connection alive (`ESTAB 127.0.0.1:56883 ↔ 127.0.0.1:1883`); broker had
  bounced once during the lifetime of the process (Apr 27 22:42:52 UTC). STT
  re-subscribes inside `on_connect`, so MQTT reconnect was not the failure mode
  here.
- The `parec` PulseAudio source-output reported `Buffer Latency: 1658583 µsec`
  (1.66 s). **This is not a smoking gun** — a freshly restarted parec on the
  same null-sink monitor reports the same exact value. It appears to be a
  static reporting artifact of `module-null-sink`'s monitor source, not a
  consumer backlog.
- No `Stream error`, `parec exited unexpectedly`, `Recognizer reset`, or
  `Force-final` log lines fired during the failure window. The recognizer was
  never reset between Apr 27 15:24:18 (last manual reset) and the May 7
  restart — ~10 days of continuous lifetime on the same `KaldiRecognizer`
  instance.

## Activity gap

Journal shows zero STT records from Apr 29 → May 6 (9 days). Most plausibly
no one was in the VC, so the loopback monitor delivered silence and Vosk
simply emitted no finals (which is correct behavior — Vosk doesn't finalize
on uninterrupted silence under the model's endpointing rules). The recognizer
ran on silence for ~9 days straight, then suddenly received normal-energy
speech.

## Stack details

| Component | Value |
| --- | --- |
| Vosk Python | 0.3.45 |
| Model | `data/vosk-model/` — Alpha Cephei "US English mobile" (2020-12-08); Speed 0.11×RT, Latency 0.15 s |
| Decoding params (model.conf) | `min-active=200 max-active=3000 beam=10.0 lattice-beam=2.0 acoustic-scale=1.0 frame-subsampling-factor=3` |
| Endpointing | `endpoint.silence-phones=1:2:3:4:5:6:7:8:9:10`, `rule2.min-trailing-silence=0.5`, `rule3=0.75`, `rule4=1.0` |
| Sample rate | 16 kHz mono s16le |
| Audio source | PulseAudio `loopback-inject.monitor` (null-sink monitor) |
| Host | Odroid M1S, aarch64, 8 cores, 7.7 GiB RAM, Ubuntu 24.04, kernel 5.10.0-odroid-arm64 |

Recognizer is constructed once at service start (`bush_stt/transcriber.py`):

```python
class SpeechToText:
    def __init__(self, model_path, sample_rate):
        self.model = Model(model_path)
        self.recognizer = KaldiRecognizer(self.model, sample_rate)

    def accept_audio(self, audio_bytes):
        if self.recognizer.AcceptWaveform(audio_bytes):
            ...
```

The main loop only reconstructs the recognizer on `force_finalize` or
`reset_recognizer` MQTT events. Neither fired automatically over the 10-day
period.

## Suspected cause

Long-lived `KaldiRecognizer` state corruption / drift. The two state-bearing
components inside Kaldi's online decoder that could plausibly degrade after
9 days of silence + sudden speech:

1. **iVector adaptation.** Vosk's iVector extractor adapts speaker/channel
   characteristics online. Days of pure silence likely drove the iVector to
   a degenerate point that biases the acoustic model toward modeling normal
   speech as background noise — i.e., the recognizer "thinks" the channel
   has no speech, so any energy decodes to short filler tokens.
2. **Lattice / decoding state.** The online decoder maintains active states.
   If endpointing never fires (silence for days), the lattice may grow or
   skew in ways the decoder doesn't recover from when speech finally arrives.

Either is plausible; iVector drift fits the symptom (single-syllable filler
output regardless of input content) better than lattice issues. I have not
verified against Vosk/Kaldi source.

## Recommendations for the engineer

These are options, not a ranked plan — pick whatever you think is right.

- **Periodic recognizer recreation.** The cheapest fix: `stt.recognizer =
  KaldiRecognizer(stt.model, SAMPLE_RATE)` is already used on the
  `force-finalize` and `reset` paths. Trigger it on a timer (e.g., every N
  hours of wall-clock or every M minutes of cumulative silence) so state
  cannot age past a known-safe horizon. Cheap because the `Model` object
  isn't re-loaded.
- **Reset on prolonged-silence threshold.** Detect long silence at the audio
  loop level (no non-silence chunks for X minutes → reset recognizer). This
  bounds the worst case directly without speculative timers.
- **Watchdog on output quality.** If N consecutive Finals are single-token
  and ≤4 chars, recreate the recognizer. Self-healing without needing to
  diagnose the underlying Kaldi state.
- **Service-level periodic restart.** `systemctl` `RuntimeMaxSec=` or a
  systemd timer if the in-process fix is too risky to pursue. Crude but
  reliable — STT init takes <2 s, no audio is lost long-term.
- **Diagnostic logging to add now**, regardless of fix path: log Vosk
  version, recognizer-creation timestamp, and a periodic heartbeat with
  total bytes processed + finals emitted. Would have made this report
  much shorter to write.

## Reproduction

I did not attempt offline repro. The natural setup would be: feed the
recognizer 9 days of silence (or fast-forward equivalent — many hours of
synthesized white-noise-floor audio), then feed real speech, and check
whether `'huh'` outputs dominate. If reliable, that's the bug to fix.
Note that real speech *did* occasionally make it through during the
failure window (not seen in this run, but it's how degradation has shown
up in other Vosk reports), so any repro should accept "majority garbage"
not "100 % garbage" as a positive signal.

# Bush Glue

**Pipeline:** `bush-stt` → `bush-t2v` → `bush-tts` + `bush-sentiment` → fire/audio

## Commands

```bash
bushctl restart stt        # restart a service  (stt, t2v, tts, sentiment, audio-agent)
bushctl status             # all pipeline services
bush-monitor               # live TUI
bush-pray --phrase "..."   # inject a phrase into the pipeline
bush-integration-test      # end-to-end smoke test
```

## Tweaking

| | File | Knobs |
|--|------|-------|
| STT | `stt-service.py` | model, input device |
| t2v | `t2v-service.py` | embed model, ChromaDB collection |
| TTS | `tts-service.py` | `ESPEAK_CMD` (voice/speed/pitch), `SOX_EFFECTS` (reverb/gain) |

After editing: `bushctl restart <service>` — logs: `journalctl -q -u bush-<service> -f`

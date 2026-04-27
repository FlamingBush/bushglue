---
name: deploy
description: Deploy bushglue to the odroid and run the integration test. Use this after making any changes that could break the pipeline — service file edits, script changes, new dependencies, audio routing changes, etc.
user-invocable: true
allowed-tools: Bash
---

Deploy the current state of the bushglue repo to the odroid and verify the pipeline end-to-end.

## Steps

1. **Push** the local repo to origin:
   ```
   cd /Users/marcus/bush-integration/bushglue && git push origin main
   ```

2. **Pull on odroid and sync dependencies**:
   ```
   ssh odroid 'cd ~/bushglue && git pull && ~/.local/bin/uv sync --all-packages'
   ```

3. **Check if any systemd service files changed** in the most recent push. If any `systemd/odroid/*.service` files were modified, deploy them:
   ```
   ssh odroid 'sudo cp ~/bushglue/systemd/odroid/*.service /etc/systemd/system/ && sudo systemctl daemon-reload'
   ```

4. **Check if any Pico 2 firmware files changed.** The relay-control Pico 2 W (CIRCUITPY) is auto-mounted at `/mnt/pico` with `uid=1000,gid=1000` (see fstab), so the odroid user can write to it without sudo. If any `firmware/relay-control/CIRCUITPY/*.py` files changed in the pull, copy them up:
   ```
   ssh odroid 'cp ~/bushglue/firmware/relay-control/CIRCUITPY/code.py ~/bushglue/firmware/relay-control/CIRCUITPY/valve.py /mnt/pico/ && sync'
   ```
   Do NOT copy `secrets.example.py` — the Pico has its own `secrets.py` with real wifi creds. CircuitPython auto-restarts on file write, so wait ~8s for it to reconnect to MQTT before testing. Verify it's back by waiting for a status publish:
   ```
   ssh odroid 'timeout 15 mosquitto_sub -h localhost -t "bush/fire/valve/status" -C 1'
   ```

5. **Restart affected services.** Restart whichever services had their script or service file changed. If unsure, restart all pipeline services:
   ```
   ssh odroid 'sudo systemctl restart bush-stt bush-tts bush-t2v bush-sentiment bush-audio-agent'
   ```
   Wait a few seconds for services to settle before running the test.

6. **Restart the local monitor** (if it's running, it will reload with the new code):
   ```
   ssh odroid 'mosquitto_pub -h localhost -t bush/monitor/restart -m "{}"'
   ```

7. **Run the audio health check**:
   ```
   ssh odroid 'bush-audio-fix'
   ```
   Report any FAILs. Any FAIL is a real problem.

8. **Run the integration test** with a 40-second timeout:
   ```
   ssh odroid 'timeout 40 ~/bushglue/.venv/bin/python ~/bushglue/utils/bush-integration-test'
   ```
   If it exits with code 124, the test timed out — treat that as a failure and diagnose accordingly.

## Pass/fail

- If the integration test passes, report success (and note any audio health warnings separately).
- If the integration test fails, show the output and diagnose the failure based on which stage failed:
  - `stt/transcript` — STT or loopback audio issue; check `journalctl -u bush-stt -n 20`
  - `t2v/verse` — t2v or Ollama issue; check `journalctl -u bush-t2v -n 20`
  - `tts/speaking` or `tts/done` — TTS issue; check `journalctl -u bush-tts -n 20`
  - `sentiment/result` or `flare pulse` — sentiment service issue; check `journalctl -u bush-sentiment -n 20`
  - `tts/done` gap check — sox/espeak finished too fast, possible device or audio routing issue

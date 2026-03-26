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

2. **Pull on odroid**:
   ```
   ssh odroid-cmd 'cd ~/repos/bushglue && git pull'
   ```

3. **Check if any systemd service files changed** in the most recent push. If any `systemd/odroid/*.service` files were modified, deploy them:
   ```
   ssh odroid-cmd 'sudo cp ~/repos/bushglue/systemd/odroid/*.service /etc/systemd/system/ && sudo systemctl daemon-reload'
   ```

4. **Restart affected services.** Restart whichever services had their script or service file changed. If unsure, restart all pipeline services:
   ```
   ssh odroid-cmd 'sudo systemctl restart bush-stt bush-tts bush-t2v bush-sentiment bush-audio-agent'
   ```
   Wait a few seconds for services to settle before running the test.

5. **Restart the local monitor** (if it's running, it will reload with the new code):
   ```
   ssh odroid-cmd 'mosquitto_pub -h localhost -t bush/monitor/restart -m "{}"'
   ```

6. **Run the audio health check**:
   ```
   ssh odroid-cmd 'bush-audio-fix'
   ```
   Report any FAILs. The USB codec being in broken state is a known hardware issue (needs replug) — flag it but do not treat it as a deploy failure. Any other FAIL is a real problem.

7. **Run the integration test** with a 40-second timeout:
   ```
   ssh odroid-cmd 'timeout 40 bush-integration-test'
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

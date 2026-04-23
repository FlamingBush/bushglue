Import ./PLAN.md and implement the motorized needle valve subsystem described there.

Before writing any code:

1. Read PLAN.md in full.
2. Read the existing repo files it references (firmware/relay-control/CIRCUITPY/code.py, services/sentiment/, MQTT.md, PROJECT.md, mqtt-architecture.dot, systemd/, INSTALL.md, the bush and bushctl scripts, and any other top-level Python services you need to understand conventions).
3. Fetch the MKS SERVO42C-MT V1.1 UART command syntax from Makerbase's GitHub (start at github.com/makerbase-mks and find the SERVO42C repo and wiki). The command table in PLAN.md section 2 is conceptual — replace it with the actual MKS command syntax in PROTOCOL.md.
4. Post a short summary in chat covering:
   - Current relay-control firmware structure, free GPIO pins, and MQTT conventions you observed
   - What the sentiment service actually publishes (topics, payload shape, rate)
   - Whether TTS publishes word-level timing or only start/end events
   - Which MKS command syntax you'll be targeting
   - Any open questions from PLAN.md section "Open Questions / Assumptions to Verify" that you cannot answer from the code alone
5. Wait for my confirmation before implementing.

Then implement in the order listed in PLAN.md section "Implementation Order". Follow existing repo conventions for code style, naming, logging, and MQTT topic structure — do not invent new patterns where the repo already has one.

Out-of-scope items in PLAN.md are genuinely out of scope; do not expand them. If something in the plan contradicts what you find in the repo, flag it and ask rather than silently diverging.

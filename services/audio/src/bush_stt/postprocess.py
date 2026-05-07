"""Optional LLM post-correction for STT transcripts.

Calls an Ollama HTTP endpoint with a domain-aware prompt to correct
common ASR errors ('burning bus' -> 'burning bush', etc.). Opt-in via
STT_LLM_CORRECT=1 env var.

Architecture: stateless function. The bush-stt main loop calls
correct_transcript(raw_text) -> corrected_text after engine.transcribe.
On any timeout/error, returns the raw text unchanged. Never raises.

Reference: original implementation in middog/bushglue commit ff29f2c
(Apr 2 2026, pre-uv-workspace era).
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request


OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://localhost:11434/api/generate")
LLM_CORRECT_MODEL = os.environ.get("STT_LLM_CORRECT_MODEL", "qwen3:0.6b")
LLM_CORRECT_TIMEOUT_S = float(os.environ.get("STT_LLM_CORRECT_TIMEOUT_S", "2.0"))


_CORRECTION_PROMPT = """\
You are correcting a speech-to-text transcript from a fire-art installation.
The speaker is talking to an interactive burning bush at Burning Man.

Common ASR errors to fix:
- 'burning bus' / 'burning bush' (always 'burning bush')
- biblical names: Moses, Jacob, Sarah
- 'desert' vs 'deserts' vs 'dessert'
- 'flame', 'fire', 'wilderness' should be transcribed correctly

Return ONLY the corrected transcript, no explanation, no quotes, no extra words.
If the input is already correct, return it verbatim.

Input: {transcript}
Corrected:"""


def log(msg: str) -> None:
    print(f"[stt-postprocess] {msg}", flush=True)


def correct_transcript(text: str, *, enabled: bool | None = None) -> str:
    """Run optional LLM correction. Returns input unchanged on timeout/error."""
    if enabled is None:
        enabled = os.environ.get("STT_LLM_CORRECT", "0") not in ("0", "false", "False", "")
    if not enabled or not text:
        return text
    try:
        body = json.dumps({
            "model": LLM_CORRECT_MODEL,
            "prompt": _CORRECTION_PROMPT.format(transcript=text),
            "stream": False,
            "options": {"temperature": 0.0},
        }).encode("utf-8")
        req = urllib.request.Request(
            OLLAMA_URL,
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=LLM_CORRECT_TIMEOUT_S) as resp:
            data = json.loads(resp.read())
        corrected = (data.get("response") or "").strip()
        if not corrected:
            return text
        # Defense-in-depth: never let the LLM massively rewrite the transcript.
        if len(corrected) > 3 * len(text) + 30 or len(corrected) * 3 < len(text):
            log(f"correction looked off, falling back to raw: {corrected!r}")
            return text
        return corrected
    except (urllib.error.URLError, OSError, TimeoutError) as e:
        log(f"correction timed out/errored ({type(e).__name__}); using raw")
        return text
    except Exception as e:
        log(f"correction unexpected error: {type(e).__name__}: {e}; using raw")
        return text

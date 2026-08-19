"""Claude Code fallback for the LLM layers, shared by main.py (field extraction)
and llm_filtration.py (relevancy scoring).

When every Gemini key is rate-limited — or none is configured — these helpers let
the pipeline keep going by shelling out to the `claude` CLI in headless mode
instead of dropping the tender. No API key is needed: it runs on the local Claude
Code login (Pro/Max), the same pattern as the LinkedIn DM tool's generator.py.

Trade-off worth knowing: this draws on the Claude subscription's usage limits,
which are shared with interactive Claude Code use. Extraction/scoring are simple
structured tasks, so CLAUDE_MODEL defaults to Haiku to keep it light.

Env knobs (all optional):
    USE_CLAUDE_FALLBACK=0     disable entirely; restore the old drop-the-tender behaviour
    CLAUDE_MODEL=haiku        model for the fallback; "" uses Claude Code's default
    CLAUDE_TIMEOUT=180        per-call timeout in seconds
    CLAUDE_MAX_CONCURRENCY=4  cap on simultaneous `claude` processes

This module lives apart from main.py on purpose: llm_filtration.py needs it too,
and main.py already imports llm_filtration, so importing back would be circular.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import threading

USE_CLAUDE_FALLBACK = os.environ.get("USE_CLAUDE_FALLBACK", "1").strip() not in ("0", "false", "False")
CLAUDE_MODEL = os.environ.get("CLAUDE_MODEL", "haiku").strip()
CLAUDE_TIMEOUT = int(os.environ.get("CLAUDE_TIMEOUT", "180"))

# `claude --print` spawns a CLI process per call, and the scraper runs one thread
# per adapter — without a bound we could fan out a process per adapter at once.
# Calls queue on this semaphore rather than failing.
_claude_semaphore = threading.Semaphore(int(os.environ.get("CLAUDE_MAX_CONCURRENCY", "4")))

# Latched once the CLI turns out to be missing, so we stop retrying per tender.
_claude_unavailable = threading.Event()


def claude_available() -> bool:
    """True if the fallback is enabled and hasn't already failed as missing."""
    return USE_CLAUDE_FALLBACK and not _claude_unavailable.is_set()


def run_claude(prompt: str) -> str:
    """
    Run one headless `claude` generation and return its text output, or "" on
    failure. Uses `claude --print --output-format json`, which runs on the local
    Claude Code login. The model's text comes back under the `result` key.
    """
    if not claude_available():
        return ""

    cmd = ["claude", "--print", "--output-format", "json"]
    if CLAUDE_MODEL:  # empty => Claude Code's configured default
        cmd += ["--model", CLAUDE_MODEL]
    cmd.append(prompt)

    with _claude_semaphore:
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=CLAUDE_TIMEOUT)
        except FileNotFoundError:
            # Latch off — every subsequent tender would fail identically.
            _claude_unavailable.set()
            print("[Claude] `claude` CLI not found — install Claude Code and run `claude` "
                  "once to log in. Disabling the fallback for this run.")
            return ""
        except subprocess.TimeoutExpired:
            print(f"[Claude] Timed out after {CLAUDE_TIMEOUT}s.")
            return ""

    if proc.returncode != 0:
        print(f"[Claude] Exited {proc.returncode}: {(proc.stderr or '').strip()[:200]}")
        return ""

    try:
        envelope = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return (proc.stdout or "").strip()  # --output-format json not honoured

    if envelope.get("is_error"):
        print(f"[Claude] Error: {str(envelope.get('result', ''))[:200]}")
        return ""
    return (envelope.get("result") or "").strip()


def parse_llm_json(raw_text: str, tag: str) -> dict:
    """
    Pull the first JSON object out of an LLM response. Shared by both providers:
    Gemini is asked for raw JSON, but Claude tends to wrap it in ```json fences,
    and the enclosing-object regex absorbs either.
    """
    raw_text = re.sub(r"<think>.*?</think>", "", raw_text or "", flags=re.DOTALL).strip()
    match = re.search(r"\{[\s\S]*\}", raw_text)
    if not match:
        print(f"[{tag}] No JSON object found in response: {raw_text[:300]}")
        return {}
    try:
        return json.loads(match.group())
    except json.JSONDecodeError as e:
        print(f"[{tag}] JSON parse failed: {e}\nRaw: {match.group()[:300]}")
        return {}


def extract_with_claude(prompt: str) -> dict:
    """Run `prompt` through Claude and parse a JSON object out of the reply."""
    if not claude_available():
        return {}
    raw_text = run_claude(prompt)
    if not raw_text:
        return {}
    print(f"[Claude RAW RESPONSE]:\n{repr(raw_text[:500])}")
    return parse_llm_json(raw_text, "Claude")

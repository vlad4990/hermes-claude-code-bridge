#!/usr/bin/env python3
"""Hermes webhook `script:` filter for the ccb-ask / ccb-notify routes.

Installed to ~/.hermes/scripts/ccb-filter.py by install.sh (Hermes requires filter scripts
to live there). Receives the webhook payload as JSON on stdin.

Contract (see Hermes docs, "Script Filters and Transforms"):
  - print "[SILENT]"            → webhook ignored, no agent turn, no delivery
  - print a JSON object         → replaces the payload used for prompt / deliver_extra
  - nonzero exit / empty stdout → ignored

This is the last deterministic gate before Hermes spends tokens. Rules:
  1. Debounce marker-less Stop events: if the state file says the session is no longer
     awaiting (a new prompt came in), stay silent.
  2. Per-session rate limit: at most one "ask" delivery per 60 s.
  3. Trim the tail to the last 30 lines and strip ANSI noise.
"""
import json
import os
import re
import sys
import time
from pathlib import Path

STATE_DIR = Path(os.environ.get("CCB_STATE_DIR", "~/.cache/ccb/sessions")).expanduser()
RATE_DIR = Path("~/.cache/ccb/rate").expanduser()
RATE_WINDOW = 60
ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")


def silent(reason: str) -> None:
    sys.stderr.write(f"ccb-filter: silent ({reason})\n")
    print("[SILENT]")
    raise SystemExit(0)


def main() -> None:
    try:
        payload = json.load(sys.stdin)
    except json.JSONDecodeError:
        silent("bad json")

    name = payload.get("name")
    event = payload.get("event")
    if not name or event not in {"ask", "notify"}:
        silent("missing name/event")

    # 1. consistency with current state
    state_file = STATE_DIR / f"{name}.json"
    if state_file.exists():
        try:
            st = json.loads(state_file.read_text())
        except json.JSONDecodeError:
            st = {}
        if event == "ask" and not str(st.get("status", "")).startswith("awaiting"):
            silent("session no longer awaiting")

    # 2. rate limit (ask only; notify is already once-per-terminal-state)
    if event == "ask":
        RATE_DIR.mkdir(parents=True, exist_ok=True)
        stamp = RATE_DIR / f"{name}.ask"
        now = time.time()
        if stamp.exists() and now - stamp.stat().st_mtime < RATE_WINDOW:
            silent("rate limited")
        stamp.touch()

    # 3. tidy the tail for the prompt template
    tail = ANSI.sub("", payload.get("tail") or "")
    lines = [ln.rstrip() for ln in tail.splitlines() if ln.strip()]
    payload["tail"] = "\n".join(lines[-30:])
    payload["question"] = (payload.get("question") or "")[:1500]

    print(json.dumps(payload, ensure_ascii=False))


if __name__ == "__main__":
    main()

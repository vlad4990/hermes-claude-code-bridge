#!/usr/bin/env python3
"""Hermes webhook `script:` filter for the ccb-ask / ccb-notify routes.

Installed to ~/.hermes/scripts/ccb-filter.py by install.sh (Hermes only runs filter scripts
from that directory). Receives the ccb event payload (references/event-contract.md) as JSON
on stdin.

Contract ("Script Filters and Transforms" in the Hermes webhook docs):
  - print "[SILENT]"            → webhook ignored: no agent turn, no delivery
  - print a JSON object         → replaces the payload used for prompt / deliver_extra
  - nonzero exit / empty stdout → ignored

Rules — the last deterministic gate before Hermes spends tokens or pings a human:
  1. Schema check: unknown payloads are dropped.
  2. `ask` events are delivered once per request_id, and only while the session's state file
     still shows that request as pending (a stale retry after the human already answered in
     the terminal stays silent).
  3. The tail is ANSI-stripped and trimmed; long fields are capped so prompts stay small.
"""
import json
import os
import re
import sys
import time
from pathlib import Path

STATE_DIR = Path(os.environ.get("CCB_STATE_DIR", "~/.cache/ccb/sessions")).expanduser()
SEEN_DIR = Path(os.environ.get("CCB_FILTER_DIR", "~/.cache/ccb/filter")).expanduser()
SEEN_TTL = 6 * 3600
ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
TAIL_LINES = 30


def silent(reason: str) -> None:
    sys.stderr.write(f"ccb-filter: silent ({reason})\n")
    print("[SILENT]")
    raise SystemExit(0)


def main() -> None:
    try:
        payload = json.load(sys.stdin)
    except json.JSONDecodeError:
        silent("bad json")
    if not isinstance(payload, dict) or payload.get("schema") != 1:
        silent("not a ccb v1 payload")
    name, event, kind = payload.get("name"), payload.get("event"), payload.get("kind")
    if not name or event not in ("ask", "notify") or not kind:
        silent("missing name/event/kind")

    if event == "ask":
        req = payload.get("request_id")
        if not req:
            silent("ask without request_id")
        st = {}
        state_file = STATE_DIR / f"{name}.json"
        if state_file.exists():
            try:
                st = json.loads(state_file.read_text())
            except json.JSONDecodeError:
                st = {}
        pend = (st or {}).get("pending") or {}
        if st and pend.get("id") != req:
            silent("request no longer pending")
        SEEN_DIR.mkdir(parents=True, exist_ok=True)
        stamp = SEEN_DIR / f"{name}.{req}"
        if stamp.exists():
            silent("request already delivered")
        stamp.touch()
        _prune(SEEN_DIR)

    tail = ANSI.sub("", payload.get("tail") or "")
    lines = [ln.rstrip() for ln in tail.splitlines() if ln.strip()]
    payload["tail"] = "\n".join(lines[-TAIL_LINES:])
    for key in ("text", "last_message"):
        if isinstance(payload.get(key), str):
            payload[key] = payload[key][:3000]
    print(json.dumps(payload, ensure_ascii=False))


def _prune(d: Path) -> None:
    cutoff = time.time() - SEEN_TTL
    try:
        for f in d.iterdir():
            if f.stat().st_mtime < cutoff:
                f.unlink()
    except OSError:
        pass


if __name__ == "__main__":
    main()

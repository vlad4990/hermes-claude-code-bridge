#!/usr/bin/env python3
"""ccb — Claude Code Bridge CLI.

Owns tmux sessions running Claude Code, keeps per-session state files, and acts as the
Claude Code hook entry point that pushes events to Hermes' webhook adapter.

Stdlib only. No LLM anywhere in this file — that is the point.

Layout of a state file: see docs/DESIGN.md §3.2.
Event → route rules:  see docs/DESIGN.md §3.3 and references/hook-events.md.
"""
from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path

# ----------------------------------------------------------------------------- config

CONFIG_PATH = Path(os.environ.get("CCB_CONFIG", "~/.config/ccb/config.json")).expanduser()
DEFAULTS = {
    "webhook_url": "http://127.0.0.1:8644",
    "webhook_secret": "",
    "state_dir": "~/.cache/ccb/sessions",
    "raw_dir": "~/.cache/ccb/raw",
    "raw_log": True,
    "tmux_prefix": "ccb-",
    "claude_cmd": "claude",
    "claude_args": [],
    "send_delay": 0.3,
    "stop_debounce": 20,
    "tail_lines": 30,
}
MARKER_RE = re.compile(r"\[\[CCB:(NEED_INPUT|DONE|ROUND:\d+)\]\]")


def load_config() -> dict:
    cfg = dict(DEFAULTS)
    if CONFIG_PATH.exists():
        try:
            cfg.update(json.loads(CONFIG_PATH.read_text()))
        except json.JSONDecodeError as e:
            sys.exit(f"ccb: bad config {CONFIG_PATH}: {e}")
    env_secret = os.environ.get("CCB_WEBHOOK_SECRET")
    if env_secret:
        cfg["webhook_secret"] = env_secret
    cfg["state_dir"] = Path(cfg["state_dir"]).expanduser()
    cfg["raw_dir"] = Path(cfg["raw_dir"]).expanduser()
    return cfg


CFG = load_config()


def now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ----------------------------------------------------------------------------- state


def state_path(name: str) -> Path:
    return CFG["state_dir"] / f"{name}.json"


def read_state(name: str) -> dict | None:
    p = state_path(name)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text())
    except json.JSONDecodeError:
        return None


def write_state(state: dict) -> None:
    CFG["state_dir"].mkdir(parents=True, exist_ok=True)
    state["updated_at"] = now()
    p = state_path(state["name"])
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2, ensure_ascii=False))
    os.replace(tmp, p)


def all_states() -> list[dict]:
    if not CFG["state_dir"].exists():
        return []
    out = []
    for p in sorted(CFG["state_dir"].glob("*.json")):
        try:
            out.append(json.loads(p.read_text()))
        except json.JSONDecodeError:
            continue
    return out


def new_state(name: str, cwd: str) -> dict:
    return {
        "name": name,
        "tmux": CFG["tmux_prefix"] + name,
        "cwd": cwd,
        "claude_session_id": None,
        "transcript_path": None,
        "status": "starting",
        "question": None,
        "permission": None,
        "last_message": None,
        "marker": None,
        "last_event": None,
        "last_event_at": None,
        "started_at": now(),
        "updated_at": None,
        "notified": {},
        "pending_stop": None,
    }


# ----------------------------------------------------------------------------- tmux


def tmux(*args: str, check: bool = True, capture: bool = False) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(["tmux", *args], check=check, text=True,
                              capture_output=capture)
    except FileNotFoundError:
        if check:
            sys.exit("ccb: tmux not found on PATH")
        return subprocess.CompletedProcess(["tmux", *args], 127, "", "tmux not found")


def tmux_has(session: str) -> bool:
    return tmux("has-session", "-t", session, check=False, capture=True).returncode == 0


def tmux_capture(session: str, lines: int) -> str:
    r = tmux("capture-pane", "-p", "-t", session, "-S", f"-{lines}", check=False, capture=True)
    return r.stdout.rstrip("\n") if r.returncode == 0 else ""


def tmux_type(session: str, text: str) -> None:
    """Send text and Enter as two separate key events (Claude Code drops a combined \\n)."""
    tmux("send-keys", "-t", session, "-l", text)
    time.sleep(CFG["send_delay"])
    tmux("send-keys", "-t", session, "Enter")


def wait_for_prompt(session: str, timeout: int = 30) -> bool:
    """Poll the pane until Claude Code's input prompt is visible."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        screen = tmux_capture(session, 10)
        lines = [ln for ln in screen.splitlines() if ln.strip()]
        last = lines[-1] if lines else ""
        if "❯" in screen or last.lstrip().startswith(">"):
            return True
        time.sleep(0.5)
    return False


# ----------------------------------------------------------------------------- webhook


def post_webhook(route: str, payload: dict) -> tuple[bool, str]:
    url = f"{CFG['webhook_url'].rstrip('/')}/webhooks/{route}"
    body = json.dumps(payload).encode()
    ts = str(int(time.time()))
    headers = {
        "Content-Type": "application/json",
        "X-Request-ID": payload.get("event_id", str(uuid.uuid4())),
        "X-Webhook-Timestamp": ts,
    }
    secret = CFG["webhook_secret"]
    if secret:
        sig = hmac.new(secret.encode(), f"{ts}.".encode() + body, hashlib.sha256).hexdigest()
        headers["X-Webhook-Signature-V2"] = sig
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=3) as r:
            return True, r.read().decode()[:200]
    except urllib.error.HTTPError as e:
        return False, f"HTTP {e.code}: {e.read().decode()[:200]}"
    except Exception as e:  # noqa: BLE001 — hook must never crash Claude Code
        return False, str(e)


# ----------------------------------------------------------------------------- transcript


def last_assistant_message(transcript_path: str | None) -> str | None:
    """Return the text of the last assistant message in a Claude Code JSONL transcript."""
    if not transcript_path:
        return None
    p = Path(transcript_path).expanduser()
    if not p.exists():
        return None
    last = None
    try:
        with p.open() as f:
            for line in f:
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if rec.get("type") != "assistant":
                    continue
                content = rec.get("message", {}).get("content", [])
                texts = [c.get("text", "") for c in content if isinstance(c, dict) and c.get("type") == "text"]
                if texts:
                    last = "\n".join(texts).strip()
    except OSError:
        return None
    return last


def extract_marker(text: str | None) -> tuple[str | None, str | None]:
    if not text:
        return None, text
    m = MARKER_RE.search(text)
    if not m:
        return None, text
    return m.group(1), MARKER_RE.sub("", text).strip()


# ----------------------------------------------------------------------------- commands


def cmd_start(a) -> None:
    cwd = str(Path(a.cwd).expanduser().resolve())
    if not Path(cwd).is_dir():
        sys.exit(f"ccb: cwd not found: {cwd}")
    st = new_state(a.name, cwd)
    if tmux_has(st["tmux"]):
        sys.exit(f"ccb: tmux session {st['tmux']} already exists (ccb stop {a.name} first)")
    tmux("new-session", "-d", "-s", st["tmux"], "-c", cwd, "-e", f"CCB_TASK={a.name}")
    claude_line = " ".join([CFG["claude_cmd"], *CFG["claude_args"], *(a.claude_args or [])])
    tmux("send-keys", "-t", st["tmux"], claude_line, "Enter")
    write_state(st)
    ready = wait_for_prompt(st["tmux"])
    if not ready:
        st["status"] = "error"
        st["last_message"] = "claude prompt did not appear within 30s"
        write_state(st)
        sys.exit(f"ccb: {st['last_message']}\n{tmux_capture(st['tmux'], 20)}")
    if a.prompt:
        tmux_type(st["tmux"], a.prompt)
    st["status"] = "running"
    write_state(st)
    print(f"started {a.name} (tmux {st['tmux']}) in {cwd}")


def cmd_send(a) -> None:
    st = read_state(a.name)
    if not st or not tmux_has(st["tmux"]):
        sys.exit(f"ccb: no live session {a.name}")
    text = a.text
    if st["status"] == "awaiting_permission":
        low = text.strip().lower()
        if low in {"y", "yes", "да", "approve", "ok", "ок"}:
            text = "y"
        elif low in {"n", "no", "нет", "deny", "reject"}:
            text = "n"
    tmux_type(st["tmux"], text)
    st.update({"status": "running", "question": None, "permission": None,
               "pending_stop": None, "notified": {}})
    write_state(st)
    print(f"[{a.name}] sent.")


def cmd_status(a) -> None:
    states = [read_state(a.name)] if a.name else all_states()
    states = [s for s in states if s]
    if a.json:
        print(json.dumps(states if not a.name else (states[0] if states else None),
                         indent=2, ensure_ascii=False))
        return
    if not states:
        print("no sessions")
        return
    for s in states:
        live = "live" if tmux_has(s["tmux"]) else "dead"
        extra = s.get("question") or (s.get("permission") or {}).get("tool_name") or s.get("marker") or ""
        print(f"{s['name']:<16} {s['status']:<20} {live:<5} {str(extra)[:60]}")


def cmd_tail(a) -> None:
    st = read_state(a.name)
    if not st:
        sys.exit(f"ccb: unknown session {a.name}")
    print(tmux_capture(st["tmux"], a.n))


def cmd_stop(a) -> None:
    st = read_state(a.name)
    if st and tmux_has(st["tmux"]):
        tmux("kill-session", "-t", st["tmux"], check=False)
    if st:
        st["status"] = "stopped"
        st["stopped_by_ccb"] = True
        write_state(st)
    print(f"stopped {a.name}")


def cmd_list(a) -> None:
    a.name = None
    a.json = False
    cmd_status(a)


# ----------------------------------------------------------------------------- hook


def cmd_hook(_a) -> None:
    """Claude Code hook entry point. Must never raise, must be fast, must exit 0."""
    try:
        _hook_inner()
    except Exception as e:  # noqa: BLE001 — a crashing hook would surface inside Claude Code
        try:
            CFG["raw_dir"].mkdir(parents=True, exist_ok=True)
            (CFG["raw_dir"] / "hook-errors.log").open("a").write(f"{now()} {e!r}\n")
        except OSError:
            pass


def _hook_inner() -> None:
    name = os.environ.get("CCB_TASK")
    if not name:
        return  # a manual claude session — not ours
    try:
        ev = json.load(sys.stdin)
    except Exception:  # noqa: BLE001
        return
    if CFG["raw_log"]:
        try:
            CFG["raw_dir"].mkdir(parents=True, exist_ok=True)
            (CFG["raw_dir"] / f"{int(time.time()*1000)}-{ev.get('hook_event_name','?')}.json").write_text(
                json.dumps(ev, indent=2, ensure_ascii=False))
        except OSError:
            pass

    st = read_state(name) or new_state(name, ev.get("cwd", ""))
    kind = ev.get("hook_event_name", "")
    st["claude_session_id"] = ev.get("session_id", st.get("claude_session_id"))
    st["transcript_path"] = ev.get("transcript_path", st.get("transcript_path"))
    st["last_event"], st["last_event_at"] = kind, now()
    event_id = f"{name}-{kind}-{int(time.time()*1000)}"
    route: str | None = None

    if kind == "PermissionRequest":
        st["status"] = "awaiting_permission"
        st["permission"] = {"tool_name": ev.get("tool_name"), "tool_input": ev.get("tool_input")}
        route = "ask"

    elif kind == "Notification":
        ntype = (ev.get("notification_type") or ev.get("type") or "").lower()
        msg = ev.get("message", "")
        if "permission" in ntype or "permission" in msg.lower():
            st["status"] = "awaiting_permission"
            route = "ask"
        elif "idle" in ntype or "waiting" in msg.lower():
            st["status"] = "awaiting_input"
            st["question"] = st.get("question") or st.get("last_message")
            route = "ask"

    elif kind == "Stop":
        if ev.get("stop_hook_active"):
            route = None
        else:
            text = last_assistant_message(st.get("transcript_path"))
            marker, clean = extract_marker(text)
            st["marker"] = marker
            st["last_message"] = (clean or "")[:2000] or None
            if marker == "DONE":
                st["status"], route = "done", "notify"
            elif marker and marker.startswith("ROUND"):
                st["status"], route = "running", None
            elif marker == "NEED_INPUT" or (clean and clean.rstrip().endswith("?")):
                st["status"], st["question"], route = "awaiting_input", clean, "ask"
            else:
                # Marker-less stop: probably waiting for the human, but give a debounce
                # window in case a follow-up turn is coming. The filter honours pending_stop.
                st["status"], st["question"] = "awaiting_input", clean
                st["pending_stop"] = now()
                route = "ask"

    elif kind == "SessionEnd":
        st["status"] = "stopped"
        route = None if st.get("stopped_by_ccb") else "notify"

    elif kind == "UserPromptSubmit":
        st.update({"status": "running", "question": None, "permission": None,
                   "pending_stop": None, "notified": {}})

    # dedup: one announcement per blocking state
    if route and st.get("notified", {}).get(route) and st["status"].startswith("awaiting"):
        route = None
    write_state(st)

    if route:
        payload = {
            "event": route,
            "event_type": route,
            "event_id": event_id,
            "name": name,
            "status": st["status"],
            "question": st.get("question"),
            "permission": st.get("permission"),
            "marker": st.get("marker"),
            "last_message": st.get("last_message"),
            "pending_stop": st.get("pending_stop"),
            "tail": tmux_capture(st["tmux"], CFG["tail_lines"]),
            "text": _notify_text(st),
        }
        ok, info = post_webhook(f"ccb-{route}", payload)
        if ok:
            st.setdefault("notified", {})[route] = event_id
            write_state(st)


def _notify_text(st: dict) -> str:
    if st["status"] == "done":
        return f"✅ [{st['name']}] done — ready to merge."
    if st["status"] == "stopped":
        return f"⏹ [{st['name']}] session ended."
    return f"[{st['name']}] {st['status']}"


# ----------------------------------------------------------------------------- doctor


def cmd_doctor(_a) -> None:
    ok = True

    def check(label: str, cond: bool, hint: str = "") -> None:
        nonlocal ok
        ok &= cond
        print(f"{'✓' if cond else '✗'} {label}" + (f" — {hint}" if not cond and hint else ""))

    check("tmux on PATH", shutil.which("tmux") is not None)
    check("claude on PATH", shutil.which(CFG["claude_cmd"]) is not None)
    check("config present", CONFIG_PATH.exists(), f"missing {CONFIG_PATH}; run install.sh")
    check("webhook secret set", bool(CFG["webhook_secret"]), "CCB_WEBHOOK_SECRET / config.webhook_secret")
    try:
        with urllib.request.urlopen(f"{CFG['webhook_url']}/health", timeout=2) as r:
            check("hermes webhook /health", r.status == 200)
    except Exception as e:  # noqa: BLE001
        check("hermes webhook /health", False, f"{e}; is the gateway running with WEBHOOK_ENABLED=true?")
    settings = Path("~/.claude/settings.json").expanduser()
    hooks_ok = settings.exists() and "ccb" in settings.read_text()
    check("claude hooks installed", hooks_ok, "run install.sh")
    filt = Path("~/.hermes/scripts/ccb-filter.py").expanduser()
    check("webhook filter in ~/.hermes/scripts", filt.exists(), "run install.sh")
    sys.exit(0 if ok else 1)


# ----------------------------------------------------------------------------- main


def main() -> None:
    p = argparse.ArgumentParser(prog="ccb", description=__doc__.splitlines()[0])
    sp = p.add_subparsers(dest="cmd", required=True)

    s = sp.add_parser("start"); s.add_argument("name"); s.add_argument("--cwd", required=True)
    s.add_argument("--prompt"); s.add_argument("--claude-args", nargs="*"); s.set_defaults(fn=cmd_start)

    s = sp.add_parser("send"); s.add_argument("name"); s.add_argument("text"); s.set_defaults(fn=cmd_send)

    s = sp.add_parser("status"); s.add_argument("name", nargs="?"); s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_status)

    s = sp.add_parser("tail"); s.add_argument("name"); s.add_argument("-n", type=int, default=40); s.set_defaults(fn=cmd_tail)
    s = sp.add_parser("stop"); s.add_argument("name"); s.set_defaults(fn=cmd_stop)
    s = sp.add_parser("list"); s.set_defaults(fn=cmd_list)
    s = sp.add_parser("hook"); s.set_defaults(fn=cmd_hook)
    s = sp.add_parser("doctor"); s.set_defaults(fn=cmd_doctor)

    a = p.parse_args()
    a.fn(a)


if __name__ == "__main__":
    main()

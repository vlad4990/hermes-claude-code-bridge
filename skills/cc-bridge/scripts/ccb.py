#!/usr/bin/env python3
"""ccb — Claude Code Bridge CLI.

Owns tmux sessions running Claude Code, keeps one state file per session, and is the
Claude Code hook entry point. When Claude Code blocks on a human (a permission prompt or an
AskUserQuestion dialog) the PermissionRequest hook publishes a structured `ask` event to
the configured sinks and waits; `ccb answer` delivers the human's reply and the hook
returns it to Claude Code as a decision — no keystrokes, no screen scraping. The TUI parser
(ccb_screen.py) is the fallback when the hook path is unavailable.

Stdlib only, Python 3.9+. No LLM anywhere in this file — that is the point.

State file:      docs/DESIGN.md §3.2            Event payloads: references/event-contract.md
Hook mapping:    references/hook-events.md      TUI fixtures:   references/tui-dialogs.md
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
import shlex
import tempfile
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent))
import ccb_screen  # noqa: E402

VERSION = "0.2.0"
CONTRACT = 1  # event payload schema, see references/event-contract.md

# ----------------------------------------------------------------------------- config

CONFIG_PATH = Path(os.environ.get("CCB_CONFIG", "~/.config/ccb/config.json")).expanduser()
DEFAULTS: Dict[str, Any] = {
    # where events go: any of "webhook" (Hermes webhook adapter), "inbox" (JSON files,
    # consumed by the Hermes plugin or anything else), "command" (your own script)
    "sinks": ["webhook"],
    "webhook_url": "http://127.0.0.1:8644",
    "webhook_secret": "",
    "webhook_routes": {"ask": "ccb-ask", "notify": "ccb-notify"},
    "command_sink": None,
    "state_dir": "~/.cache/ccb/sessions",
    "answers_dir": "~/.cache/ccb/answers",
    "inbox_dir": "~/.cache/ccb/inbox",
    "raw_dir": "~/.cache/ccb/raw",
    "raw_log": False,
    "tmux_prefix": "ccb-",
    "claude_cmd": "claude",
    "claude_args": [],
    "send_delay": 0.3,
    "key_delay": 0.25,
    # how long the PermissionRequest hook waits for `ccb answer`. Must stay below the hook
    # `timeout` in templates/claude-hooks.json (3600) so we exit cleanly, not cancelled.
    "answer_timeout": 3300,
    "answer_poll": 0.5,
    # notify | markers | silent — what a Stop (end of Claude's turn) does. See DESIGN §3.3.
    "stop_policy": "notify",
    "tail_lines": 40,
    "start_timeout": 90,
    "message_limit": 1500,
}
MARKER_RE = re.compile(r"\[\[CCB:(NEED_INPUT|DONE|ROUND:\d+)\]\]")
STATUSES_AWAITING = ("awaiting_input", "awaiting_permission")


def load_config() -> Dict[str, Any]:
    cfg: Dict[str, Any] = json.loads(json.dumps(DEFAULTS))
    if CONFIG_PATH.exists():
        try:
            user = json.loads(CONFIG_PATH.read_text())
        except json.JSONDecodeError as e:
            sys.exit(f"ccb: bad config {CONFIG_PATH}: {e}")
        if not isinstance(user, dict):
            sys.exit(f"ccb: config {CONFIG_PATH} must be a JSON object")
        routes = dict(cfg["webhook_routes"])
        routes.update(user.get("webhook_routes") or {})
        cfg.update(user)
        cfg["webhook_routes"] = routes
    env_secret = os.environ.get("CCB_WEBHOOK_SECRET")
    if env_secret:
        cfg["webhook_secret"] = env_secret
    if isinstance(cfg["sinks"], str):
        cfg["sinks"] = [cfg["sinks"]]
    for key in ("state_dir", "answers_dir", "inbox_dir", "raw_dir"):
        cfg[key] = Path(str(cfg[key])).expanduser()
    return cfg


CFG = load_config()


def now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def new_id(prefix: str) -> str:
    return f"{prefix}-{int(time.time() * 1000)}-{uuid.uuid4().hex[:6]}"


def _atomic_write(path: Path, data: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + f".{os.getpid()}.tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False))
    os.replace(tmp, path)


def _read_json(path: Path) -> Optional[Dict[str, Any]]:
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


# ----------------------------------------------------------------------------- state


def state_path(name: str) -> Path:
    return CFG["state_dir"] / f"{name}.json"


def read_state(name: str) -> Optional[Dict[str, Any]]:
    return _read_json(state_path(name)) if state_path(name).exists() else None


def write_state(state: Dict[str, Any]) -> None:
    state["updated_at"] = now()
    _atomic_write(state_path(state["name"]), state)


def all_states() -> List[Dict[str, Any]]:
    if not CFG["state_dir"].exists():
        return []
    out = []
    for p in sorted(CFG["state_dir"].glob("*.json")):
        st = _read_json(p)
        if st and st.get("name"):
            out.append(st)
    return out


def new_state(name: str, cwd: str) -> Dict[str, Any]:
    return {
        "schema": 2,
        "name": name,
        "tmux": CFG["tmux_prefix"] + name,
        "cwd": cwd,
        "claude_session_id": None,
        "transcript_path": None,
        "status": "starting",
        "pending": None,
        "last_message": None,
        "marker": None,
        "last_event": None,
        "last_event_at": None,
        "started_at": now(),
        "updated_at": None,
        "stopped_by_ccb": False,
        "notified": {},
    }


def validate_name(name: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", name or ""):
        sys.exit("ccb: session name must be 1-64 chars of letters, digits, '.', '_' or '-'")
    return name


# ----------------------------------------------------------------------------- tmux


def tmux(*args: str, check: bool = True, capture: bool = False) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(["tmux", *args], check=check, text=True, capture_output=capture)
    except FileNotFoundError:
        if check:
            sys.exit("ccb: tmux not found on PATH")
        return subprocess.CompletedProcess(["tmux", *args], 127, "", "tmux not found")
    except subprocess.CalledProcessError as e:
        sys.exit(f"ccb: tmux {' '.join(args[:2])} failed: {(e.stderr or '').strip()}")


def tmux_has(session: str) -> bool:
    return tmux("has-session", "-t", session, check=False, capture=True).returncode == 0


def tmux_capture(session: str, lines: int) -> str:
    """Last `lines` screen lines with wrapped lines re-joined (-J) — the parser relies on it."""
    r = tmux("capture-pane", "-p", "-J", "-t", session, "-S", f"-{lines}", check=False, capture=True)
    return r.stdout.rstrip("\n") if r.returncode == 0 else ""


def tmux_type(session: str, text: str) -> None:
    """Send text and Enter as two key events (Claude Code drops a newline glued to a paste)."""
    tmux("send-keys", "-t", session, "-l", text)
    time.sleep(CFG["send_delay"])
    tmux("send-keys", "-t", session, "Enter")


def tmux_keys(session: str, plan: List[Dict[str, str]]) -> None:
    for tok in plan:
        if "text" in tok:
            tmux("send-keys", "-t", session, "-l", tok["text"])
        else:
            tmux("send-keys", "-t", session, tok["key"])
        time.sleep(CFG["key_delay"])


def screen_of(session: str) -> Dict[str, Any]:
    return ccb_screen.parse_screen(tmux_capture(session, CFG["tail_lines"]))


# ----------------------------------------------------------------------------- sinks


def post_webhook(route: str, payload: Dict[str, Any]) -> Tuple[bool, str]:
    url = f"{str(CFG['webhook_url']).rstrip('/')}/webhooks/{route}"
    body = json.dumps(payload, ensure_ascii=False).encode()
    ts = str(int(time.time()))
    headers = {
        "Content-Type": "application/json",
        "X-Request-ID": payload["event_id"],
        "X-Webhook-Timestamp": ts,
        "User-Agent": f"ccb/{VERSION}",
    }
    secret = CFG["webhook_secret"]
    if secret:
        sig = hmac.new(secret.encode(), f"{ts}.".encode() + body, hashlib.sha256).hexdigest()
        headers["X-Webhook-Signature-V2"] = sig
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return True, r.read().decode()[:200]
    except urllib.error.HTTPError as e:
        return False, f"HTTP {e.code}: {e.read().decode()[:200]}"
    except Exception as e:  # noqa: BLE001 — a sink failure must never crash a hook
        return False, str(e)


def emit(payload: Dict[str, Any]) -> Dict[str, str]:
    """Deliver one event to every configured sink. Returns {sink: result}. Never raises."""
    results: Dict[str, str] = {}
    for sink in CFG["sinks"]:
        try:
            if sink == "webhook":
                route = CFG["webhook_routes"].get(payload["event"], f"ccb-{payload['event']}")
                ok, info = post_webhook(route, payload)
                results[sink] = ("ok " if ok else "FAIL ") + info
            elif sink == "inbox":
                CFG["inbox_dir"].mkdir(parents=True, exist_ok=True)
                _atomic_write(CFG["inbox_dir"] / f"{payload['event_id']}.json", payload)
                results[sink] = "ok"
            elif sink == "command":
                cmd = CFG.get("command_sink")
                if not cmd:
                    results[sink] = "FAIL command_sink not configured"
                    continue
                r = subprocess.run(cmd, shell=True, input=json.dumps(payload, ensure_ascii=False),
                                   text=True, capture_output=True, timeout=10)
                results[sink] = "ok" if r.returncode == 0 else f"FAIL exit {r.returncode}: {r.stderr[:200]}"
            else:
                results[sink] = "FAIL unknown sink"
        except Exception as e:  # noqa: BLE001
            results[sink] = f"FAIL {e}"
    _log(f"emit {payload['event']}/{payload['kind']} {payload['name']} -> {results}")
    return results


def _log(line: str) -> None:
    try:
        CFG["raw_dir"].mkdir(parents=True, exist_ok=True)
        with (CFG["raw_dir"] / "ccb.log").open("a") as f:
            f.write(f"{now()} {line}\n")
    except OSError:
        pass


# ----------------------------------------------------------------------------- payloads


def summarize_tool_input(tool_name: str, tool_input: Any) -> str:
    ti = tool_input if isinstance(tool_input, dict) else {}
    if tool_name == "Bash":
        return str(ti.get("command") or "")
    for key in ("file_path", "path", "notebook_path", "url", "pattern", "command", "prompt", "description"):
        if ti.get(key):
            return f"{key}: {ti[key]}"
    try:
        return json.dumps(ti, ensure_ascii=False)[:300]
    except (TypeError, ValueError):
        return str(tool_input)[:300]


def normalize_questions(tool_input: Any) -> List[Dict[str, Any]]:
    out = []
    qs = tool_input.get("questions") if isinstance(tool_input, dict) else None
    for i, q in enumerate(qs or [], start=1):
        if not isinstance(q, dict):
            continue
        opts = []
        for j, o in enumerate(q.get("options") or [], start=1):
            if isinstance(o, dict):
                opts.append({"index": j, "label": str(o.get("label", "")), "description": str(o.get("description", ""))})
            else:
                opts.append({"index": j, "label": str(o), "description": ""})
        out.append({
            "index": i,
            "question": str(q.get("question", "")),
            "header": str(q.get("header", "")),
            "multi_select": bool(q.get("multiSelect")),
            "options": opts,
            "allow_free_text": True,
        })
    return out


PERMISSION_CHOICES = [
    {"index": 1, "label": "Allow", "reply": "yes"},
    {"index": 2, "label": "Allow and don't ask again", "reply": "always"},
    {"index": 3, "label": "Deny", "reply": "no"},
]


def build_pending(ev: Dict[str, Any], source: str = "hook") -> Dict[str, Any]:
    tool = str(ev.get("tool_name") or "")
    tool_input = ev.get("tool_input")
    pend: Dict[str, Any] = {
        "id": new_id("req"),
        "kind": "question" if tool == "AskUserQuestion" else "permission",
        "tool_name": tool,
        "tool_input": tool_input,
        "created_at": now(),
        "source": source,
        "hook_pid": os.getpid() if source == "hook" else None,
    }
    if pend["kind"] == "question":
        pend["questions"] = normalize_questions(tool_input)
    else:
        pend["permission"] = {
            "tool_name": tool,
            "summary": summarize_tool_input(tool, tool_input),
            "input": tool_input,
            "suggestions": ev.get("permission_suggestions") or [],
            "choices": PERMISSION_CHOICES,
        }
    return pend


def pending_from_screen(scr: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Fallback: describe an on-screen dialog as a pending request (source=screen)."""
    kind = scr.get("kind")
    if kind == "permission":
        return {
            "id": new_id("scr"), "kind": "permission", "tool_name": scr.get("title") or "tool",
            "tool_input": None, "created_at": now(), "source": "screen", "hook_pid": None,
            "permission": {"tool_name": scr.get("title") or "tool", "summary": " ".join(scr.get("detail") or []),
                           "input": None, "suggestions": [], "choices": PERMISSION_CHOICES},
        }
    if kind == "question":
        opts = [{"index": o["index"], "label": o["label"], "description": o["description"]}
                for o in scr["options"] if o["index"] and not o["special"]]
        return {
            "id": new_id("scr"), "kind": "question", "tool_name": "AskUserQuestion", "tool_input": None,
            "created_at": now(), "source": "screen", "hook_pid": None,
            "questions": [{"index": 1, "question": scr.get("question") or "", "header": scr.get("title") or "",
                           "multi_select": bool(scr.get("multi_select")), "options": opts, "allow_free_text": True}],
        }
    return None


def render_text(st: Dict[str, Any], kind: str, pend: Optional[Dict[str, Any]]) -> Tuple[str, str]:
    """Human-readable (title, text) for a payload. Delivered as-is by deliver_only routes."""
    name = st["name"]
    limit = int(CFG["message_limit"])
    if kind == "question" and pend:
        parts = []
        for q in pend["questions"]:
            head = f"{q['header']}: " if q["header"] else ""
            ms = " (choose several, e.g. 1,3)" if q["multi_select"] else ""
            lines = [f"{head}{q['question']}{ms}"]
            for o in q["options"]:
                desc = f" — {o['description']}" if o["description"] else ""
                lines.append(f"  {o['index']}. {o['label']}{desc}")
            lines.append("  or reply with free text")
            parts.append("\n".join(lines))
        title = f"❓ [{name}] Claude asks"
        return title, title + "\n" + "\n\n".join(parts)
    if kind == "permission" and pend:
        p = pend["permission"]
        title = f"🔐 [{name}] Claude wants to run {p['tool_name']}"
        body = p["summary"][:limit]
        choices = "\n".join(f"  {c['index']}. {c['label']}" for c in p["choices"])
        return title, f"{title}\n{body}\n{choices}"
    if kind == "idle":
        msg = (st.get("last_message") or "(no text)")[:limit]
        title = f"💬 [{name}] Claude finished a turn"
        return title, f"{title}\n{msg}"
    if kind == "done":
        title = f"✅ [{name}] done"
        return title, f"{title}\n{(st.get('last_message') or '')[:limit]}".rstrip()
    if kind == "stopped":
        title = f"⏹ [{name}] session ended"
        return title, title
    if kind == "error":
        title = f"⚠️ [{name}] error"
        return title, f"{title}\n{(st.get('last_message') or '')[:limit]}"
    title = f"[{name}] {kind}"
    return title, title


def build_payload(st: Dict[str, Any], event: str, kind: str, pend: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    title, text = render_text(st, kind, pend)
    payload: Dict[str, Any] = {
        "schema": CONTRACT,
        "event": event,               # ask | notify
        "event_type": event,          # same value; the Hermes webhook adapter filters routes on it
        "kind": kind,                 # question | permission | idle | done | stopped | error
        "event_id": new_id(f"{st['name']}-{kind}"),
        "request_id": pend["id"] if pend else None,
        "name": st["name"],
        "status": st["status"],
        "title": title,
        "text": text,
        "questions": pend.get("questions") if pend else None,
        "permission": pend.get("permission") if pend else None,
        "reply_hint": None,
        "answer_command": None,
        "last_message": st.get("last_message"),
        "marker": st.get("marker"),
        "tail": tmux_capture(st["tmux"], CFG["tail_lines"]) if tmux_has(st["tmux"]) else "",
        "cwd": st.get("cwd"),
        "claude_session_id": st.get("claude_session_id"),
        "at": now(),
    }
    if pend:
        if pend["kind"] == "question":
            multi = len(pend["questions"]) > 1
            payload["reply_hint"] = ("Reply per question in order, e.g. `2 | 1,3`" if multi
                                     else "Reply with an option number, several numbers (1,3) or free text")
            payload["answer_command"] = (f"ccb answer {st['name']} --q 1 <reply> --q 2 <reply>" if multi
                                         else f"ccb answer {st['name']} <reply>")
        else:
            payload["reply_hint"] = "Reply yes / always / no (or 1 / 2 / 3)"
            payload["answer_command"] = f"ccb answer {st['name']} <yes|always|no>"
    return payload


# ----------------------------------------------------------------------------- transcript / markers


def last_assistant_message(transcript_path: Optional[str]) -> Optional[str]:
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


def extract_marker(text: Optional[str]) -> Tuple[Optional[str], Optional[str]]:
    if not text:
        return None, text
    m = MARKER_RE.search(text)
    if not m:
        return None, text
    return m.group(1), MARKER_RE.sub("", text).strip()


# ----------------------------------------------------------------------------- answers (hook <-> ccb answer)


def answer_path(name: str, request_id: str) -> Path:
    return CFG["answers_dir"] / name / f"{request_id}.json"


def wait_for_answer(name: str, request_id: str) -> Optional[Dict[str, Any]]:
    """Block until `ccb answer` writes the file, the pending request disappears, or timeout."""
    deadline = time.time() + float(CFG["answer_timeout"])
    path = answer_path(name, request_id)
    next_state_check = 0.0
    while time.time() < deadline:
        if path.exists():
            ans = _read_json(path)
            try:
                path.unlink()
            except OSError:
                pass
            if ans is not None:
                return ans
        if time.time() >= next_state_check:
            st = read_state(name)
            pend = (st or {}).get("pending")
            if not st or not pend or pend.get("id") != request_id:
                return None  # answered in the TUI (PostToolUse cleared it), new prompt, or stopped
            next_state_check = time.time() + 2.0
        time.sleep(float(CFG["answer_poll"]))
    return None


def decision_for(pend: Dict[str, Any], ans: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Translate an answer file into Claude Code's PermissionRequest decision object."""
    if ans.get("release"):
        return None
    if pend["kind"] == "question":
        answers = ans.get("answers") or {}
        resolved: Dict[str, str] = {}
        for q in pend["questions"]:
            reply = answers.get(str(q["index"])) or answers.get(q["question"])
            if reply is None:
                return None  # incomplete; ccb answer validates, but never guess here
            resolved[q["question"]] = str(reply)
        updated = dict(pend.get("tool_input") or {})
        updated["answers"] = resolved
        return {"behavior": "allow", "updatedInput": updated}
    decision = ans.get("decision")
    if decision in ("allow", "always"):
        out: Dict[str, Any] = {"behavior": "allow"}
        if decision == "always" and pend["permission"].get("suggestions"):
            out["updatedPermissions"] = pend["permission"]["suggestions"]
        return out
    if decision == "deny":
        return {"behavior": "deny", "message": ans.get("message") or
                "Denied by the operator through the Hermes bridge. Explain what you needed and wait."}
    return None


def reply_to_answer(pend: Dict[str, Any], replies: List[str], per_question: List[Tuple[int, str]],
                    message: Optional[str]) -> Dict[str, Any]:
    """Validate human replies against the pending request and build the answer file content."""
    if pend["kind"] == "permission":
        if len(replies) != 1 or per_question:
            sys.exit("ccb: a permission needs exactly one reply: yes | always | no (or 1 | 2 | 3)")
        norm = ccb_screen.normalize_reply(replies[0])
        decision = norm.get("decision")
        if norm.get("options"):
            decision = {1: "allow", 2: "always", 3: "deny"}.get(norm["options"][0])
        if decision not in ("allow", "always", "deny"):
            sys.exit(f"ccb: cannot read '{replies[0]}' as yes / always / no")
        return {"decision": decision, "message": message}
    questions = pend["questions"]
    answers: Dict[str, str] = {}
    pairs: List[Tuple[int, str]] = list(per_question)
    if replies:
        if len(questions) == 1 and len(replies) == 1:
            pairs.append((1, replies[0]))
        elif len(replies) == 1 and "|" in replies[0] and len(questions) > 1:
            pairs += [(i, part.strip()) for i, part in enumerate(replies[0].split("|"), start=1)]
        elif len(replies) == len(questions):
            pairs += [(i, r) for i, r in enumerate(replies, start=1)]
        else:
            sys.exit(f"ccb: {len(questions)} question(s) pending; give one reply per question "
                     f"(--q N REPLY, or 'a | b'), got {len(replies)} reply/replies")
    for idx, reply in pairs:
        q = next((x for x in questions if x["index"] == idx), None)
        if q is None:
            sys.exit(f"ccb: no question #{idx}; pending questions: 1..{len(questions)}")
        norm = ccb_screen.normalize_reply(reply)
        if norm.get("options"):
            labels = []
            for n in norm["options"]:
                opt = next((o for o in q["options"] if o["index"] == n), None)
                if opt is None:
                    sys.exit(f"ccb: question #{idx} has options 1..{len(q['options'])}, not {n}")
                labels.append(opt["label"])
            if len(labels) > 1 and not q["multi_select"]:
                sys.exit(f"ccb: question #{idx} is single-select; pick one option")
            answers[str(idx)] = ", ".join(labels)
        else:
            answers[str(idx)] = norm.get("text") or reply
    missing = [q["index"] for q in questions if str(q["index"]) not in answers]
    if missing:
        sys.exit(f"ccb: missing reply for question(s) {missing}")
    return {"answers": answers}


# ----------------------------------------------------------------------------- commands


def cmd_start(a) -> None:
    name = validate_name(a.name)
    cwd = str(Path(a.cwd).expanduser().resolve())
    if not Path(cwd).is_dir():
        sys.exit(f"ccb: cwd not found: {cwd}")
    st = new_state(name, cwd)
    if tmux_has(st["tmux"]):
        sys.exit(f"ccb: tmux session {st['tmux']} already exists (ccb stop {name} first)")
    env = ["-e", f"CCB_TASK={name}"]
    if os.environ.get("CCB_CONFIG"):
        env += ["-e", f"CCB_CONFIG={os.environ['CCB_CONFIG']}"]
    tmux("new-session", "-d", "-s", st["tmux"], "-x", "160", "-y", "50", "-c", cwd, *env)
    extra = shlex.split(a.claude_args) if isinstance(a.claude_args, str) else list(a.claude_args or [])
    claude_line = " ".join(shlex.quote(x) for x in [CFG["claude_cmd"], *CFG["claude_args"], *extra])
    tmux("send-keys", "-t", st["tmux"], claude_line, "Enter")
    write_state(st)

    deadline = time.time() + float(CFG["start_timeout"])
    ready = False
    trusted = False
    while time.time() < deadline:
        scr = screen_of(st["tmux"])
        if scr["kind"] == "trust" and not trusted:
            tmux_keys(st["tmux"], ccb_screen.plan_keys(scr, {"decision": "allow"}))
            trusted = True
        elif scr["kind"] == "prompt":
            ready = True
            break
        elif scr["kind"] in ("permission", "question", "dialog"):
            break  # unexpected startup dialog — leave it to the human, report below
        time.sleep(0.7)
    if not ready:
        st["status"] = "error"
        st["last_message"] = f"claude prompt did not appear within {CFG['start_timeout']}s"
        write_state(st)
        sys.exit(f"ccb: {st['last_message']}\n{tmux_capture(st['tmux'], 20)}")
    if a.prompt:
        tmux_type(st["tmux"], a.prompt)
    st["status"] = "running"
    write_state(st)
    if getattr(a, "json", False):
        print(json.dumps(st, ensure_ascii=False))
    else:
        print(f"started {name} (tmux {st['tmux']}) in {cwd}")


def cmd_send(a) -> None:
    st = read_state(a.name)
    if not st or not tmux_has(st["tmux"]):
        sys.exit(f"ccb: no live session {a.name}")
    scr = screen_of(st["tmux"])
    if scr["answerable"] and not a.raw:
        sys.exit(f"ccb: {a.name} shows a dialog ({scr['kind']}). Use `ccb answer {a.name} …` "
                 f"(or --raw to type anyway).\n{ccb_screen.describe(scr)}")
    tmux_type(st["tmux"], a.text)
    st.update({"status": "running", "pending": None})
    write_state(st)
    print(f"[{a.name}] sent.")


def cmd_answer(a) -> None:
    st = read_state(a.name)
    if not st:
        sys.exit(f"ccb: unknown session {a.name}")
    per_q = [(int(n), r) for n, r in (a.q or [])]
    pend = st.get("pending")
    hook_alive = bool(pend and pend.get("source") == "hook" and _pid_alive(pend.get("hook_pid")))

    if a.release:
        if not hook_alive:
            sys.exit(f"ccb: nothing to release for {a.name}")
        _atomic_write(answer_path(a.name, pend["id"]), {"release": True, "at": now()})
        print(f"[{a.name}] released — the dialog stays in the terminal for a human.")
        return

    if hook_alive:
        ans = json.loads(a.json) if a.json else reply_to_answer(pend, a.reply, per_q, a.message)
        ans.update({"request_id": pend["id"], "kind": pend["kind"], "at": now(), "by": "ccb answer"})
        _atomic_write(answer_path(a.name, pend["id"]), ans)
        shown = ans.get("answers") or ans.get("decision")
        print(f"[{a.name}] answer delivered to Claude Code: {json.dumps(shown, ensure_ascii=False)}")
        return

    # Fallback: no hook is waiting — drive the on-screen dialog with keystrokes.
    if not tmux_has(st["tmux"]):
        sys.exit(f"ccb: no pending request and no live tmux session for {a.name}")
    scr = screen_of(st["tmux"])
    if not scr["answerable"]:
        sys.exit(f"ccb: {a.name} has no pending request and shows no dialog ({scr['kind']}). "
                 f"Use `ccb send` for a new instruction.")
    replies = list(a.reply)
    if per_q:
        replies = [r for _, r in sorted(per_q)]
    elif len(replies) == 1 and "|" in replies[0]:
        replies = [p.strip() for p in replies[0].split("|")]
    if not replies and scr["kind"] != "question_review":
        sys.exit("ccb: give a reply (option number, yes/no, or text)")
    steps = 0
    while steps < 12:
        steps += 1
        if scr["kind"] == "question_review":
            tmux_keys(st["tmux"], ccb_screen.plan_keys(scr, {}))
            break
        if not replies:
            sys.exit(f"ccb: dialog still open ({scr['kind']}: {scr.get('question')}) and no replies left")
        reply = ccb_screen.normalize_reply(replies.pop(0))
        try:
            plan = ccb_screen.plan_keys(scr, reply)
        except ValueError as e:
            sys.exit(f"ccb: {e}\n{ccb_screen.describe(scr)}")
        tmux_keys(st["tmux"], plan)
        time.sleep(0.8)
        scr = screen_of(st["tmux"])
        if not scr["answerable"]:
            break
    st.update({"status": "running", "pending": None})
    write_state(st)
    print(f"[{a.name}] answered via terminal keys; screen now: {scr['kind']}")


def _pid_alive(pid: Optional[int]) -> bool:
    if not pid:
        return False
    try:
        os.kill(int(pid), 0)
    except (OSError, ValueError):
        return False
    return True


def cmd_status(a) -> None:
    states = [read_state(a.name)] if a.name else all_states()
    states = [s for s in states if s]
    if a.json:
        print(json.dumps(states if not a.name else (states[0] if states else None), indent=2, ensure_ascii=False))
        return
    if not states:
        print("no sessions")
        return
    for s in states:
        live = "live" if tmux_has(s["tmux"]) else "dead"
        pend = s.get("pending") or {}
        extra = ""
        if pend.get("kind") == "question":
            extra = "Q: " + "; ".join(q["question"] for q in pend.get("questions", []))
        elif pend.get("kind") == "permission":
            extra = f"{pend['permission']['tool_name']}: {pend['permission']['summary']}"
        elif s.get("marker"):
            extra = s["marker"]
        elif s.get("last_message"):
            extra = s["last_message"].splitlines()[0]
        print(f"{s['name']:<16} {s['status']:<20} {live:<5} {extra[:70]}")


def cmd_screen(a) -> None:
    st = read_state(a.name)
    if not st or not tmux_has(st["tmux"]):
        sys.exit(f"ccb: no live session {a.name}")
    scr = screen_of(st["tmux"])
    if a.json:
        print(json.dumps(scr, indent=2, ensure_ascii=False))
    else:
        print(ccb_screen.describe(scr))


def cmd_tail(a) -> None:
    st = read_state(a.name)
    if not st:
        sys.exit(f"ccb: unknown session {a.name}")
    print(tmux_capture(st["tmux"], a.n))


def cmd_stop(a) -> None:
    st = read_state(a.name)
    if st:
        st["stopped_by_ccb"] = True
        write_state(st)  # before the kill so SessionEnd sees it
    if st and tmux_has(st["tmux"]):
        tmux("kill-session", "-t", st["tmux"], check=False)
    if st:
        st["status"] = "stopped"
        st["pending"] = None
        write_state(st)
    print(f"stopped {a.name}")


def cmd_list(a) -> None:
    a.name, a.json = None, False
    cmd_status(a)


def cmd_inbox(a) -> None:
    d = CFG["inbox_dir"]
    files = sorted(d.glob("*.json"), key=lambda f: f.stat().st_mtime)[-a.n:] if d.exists() else []
    if not files:
        print("inbox empty (is 'inbox' in config.sinks?)")
        return
    for f in files:
        ev = _read_json(f) or {}
        print(f"{ev.get('at','?')} {ev.get('event','?'):<6} {ev.get('kind','?'):<10} {ev.get('name','?'):<14} {str(ev.get('title',''))[:60]}")


SMOKE_PROMPT = """This is a bridge smoke test. Do exactly these steps, in order, and nothing else:
1. Call AskUserQuestion ONCE with two questions: (a) header "Color", question "Which color should the test badge use?", options Red / Green / Blue, single select; (b) header "Checks", question "Which checks should run?", options Lint / Unit tests / Type check, multiSelect true.
2. Then run with the Bash tool exactly: echo "ccb smoke: <color> / <checks>" > ccb-smoke.txt  (substitute my answers). Wait for the permission prompt; do not work around it.
3. Then call AskUserQuestion once more: header "Note", question "Any final note for the log?", options "No note" / "Add a note".
4. Finish with one line summarising what I answered."""


def cmd_demo(a) -> None:
    name = validate_name(a.name)
    if a.cwd:
        cwd = str(Path(a.cwd).expanduser().resolve())
    else:
        cwd = tempfile.mkdtemp(prefix="ccb-demo-")
        subprocess.run(["git", "init", "-q", cwd], check=False)
        Path(cwd, "README.md").write_text("# ccb demo\n")
    ns = argparse.Namespace(name=name, cwd=cwd, prompt=SMOKE_PROMPT, claude_args=a.claude_args, json=False)
    cmd_start(ns)
    # Deliberately no ready-to-run `ccb answer` lines here: an agent reading this output must
    # not be tempted to answer the smoke test itself. The walkthrough is in references/test-prompt.md.
    print(f"smoke test running as '{name}'. It will ask two questions, request one permission, ask once more, "
          f"then finish. Answers must come from the human via the chat; nothing else to do now.")


# ----------------------------------------------------------------------------- hook


def cmd_hook(_a) -> None:
    """Claude Code hook entry point. Never raises, exits 0, prints a decision JSON only when it has one."""
    try:
        out = _hook_inner()
        if out:
            print(json.dumps(out))
    except Exception as e:  # noqa: BLE001
        _log(f"hook error: {e!r}")


def _hook_inner() -> Optional[Dict[str, Any]]:
    name = os.environ.get("CCB_TASK")
    if not name:
        return None  # a manual claude session — not ours
    try:
        ev = json.load(sys.stdin)
    except Exception:  # noqa: BLE001
        return None
    kind = str(ev.get("hook_event_name") or "")
    if CFG["raw_log"]:
        try:
            CFG["raw_dir"].mkdir(parents=True, exist_ok=True)
            (CFG["raw_dir"] / f"{int(time.time() * 1000)}-{kind}.json").write_text(json.dumps(ev, indent=2, ensure_ascii=False))
        except OSError:
            pass

    st = read_state(name) or new_state(name, str(ev.get("cwd") or ""))
    st["claude_session_id"] = ev.get("session_id") or st.get("claude_session_id")
    st["transcript_path"] = ev.get("transcript_path") or st.get("transcript_path")
    st["last_event"], st["last_event_at"] = kind, now()

    if kind == "PermissionRequest":
        return _on_permission_request(st, ev)
    if kind in ("UserPromptSubmit", "PostToolUse", "PostToolUseFailure", "PermissionDenied"):
        if st.get("pending") or st["status"] != "running":
            st["pending"] = None
            st["status"] = "running"
        write_state(st)
        return None
    if kind == "Stop":
        _on_stop(st, ev)
        return None
    if kind == "Notification":
        _on_notification(st, ev)
        return None
    if kind == "SessionEnd":
        st["status"] = "stopped"
        st["pending"] = None
        st["last_message"] = f"reason: {ev.get('reason', 'unknown')}"
        write_state(st)
        if not st.get("stopped_by_ccb"):
            emit(build_payload(st, "notify", "stopped"))
        return None
    write_state(st)
    return None


def _on_permission_request(st: Dict[str, Any], ev: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    pend = build_pending(ev, source="hook")
    st["pending"] = pend
    st["status"] = "awaiting_input" if pend["kind"] == "question" else "awaiting_permission"
    write_state(st)
    payload = build_payload(st, "ask", pend["kind"], pend)
    results = emit(payload)
    st.setdefault("notified", {})[pend["id"]] = payload["event_id"]
    write_state(st)
    if not any(r.startswith("ok") for r in results.values()):
        # nobody heard us: don't hold the dialog hostage, let the terminal show it right away
        _log(f"no sink accepted ask {pend['id']}; not waiting")
        return None
    ans = wait_for_answer(st["name"], pend["id"])
    st = read_state(st["name"]) or st
    if ans is None:
        _log(f"ask {pend['id']} unanswered (timeout/release/cleared); leaving dialog to the terminal")
        return None
    decision = decision_for(pend, ans)
    if decision is None:
        return None
    st["pending"] = None
    st["status"] = "running"
    st["last_answer"] = {"request_id": pend["id"], "at": now(), "decision": decision.get("behavior"),
                         "answers": (decision.get("updatedInput") or {}).get("answers")}
    write_state(st)
    return {"hookSpecificOutput": {"hookEventName": "PermissionRequest", "decision": decision}}


def _on_stop(st: Dict[str, Any], ev: Dict[str, Any]) -> None:
    if ev.get("stop_hook_active"):
        write_state(st)
        return
    text = ev.get("last_assistant_message") or last_assistant_message(st.get("transcript_path"))
    marker, clean = extract_marker(text)
    st["marker"] = marker
    st["last_message"] = (clean or "")[:4000] or None
    st["pending"] = None
    policy = CFG["stop_policy"]
    digest = hashlib.sha1((clean or "").encode()).hexdigest()[:12]
    turn_key = f"stop-{digest}"
    already = turn_key in st.get("notified", {})

    if marker == "DONE":
        st["status"] = "done"
        kind = "done"
    elif marker and marker.startswith("ROUND"):
        st["status"] = "running"
        kind = None
    elif marker == "NEED_INPUT":
        st["status"] = "idle"
        kind = "idle"
    else:
        st["status"] = "idle"
        if policy == "notify":
            kind = "idle"
        elif policy == "markers":
            kind = "idle" if (clean or "").rstrip().endswith("?") else None
        else:
            kind = None
    write_state(st)
    if kind and not already and policy != "silent":
        payload = build_payload(st, "notify", kind)
        emit(payload)
        st.setdefault("notified", {})[turn_key] = payload["event_id"]
        write_state(st)


def _on_notification(st: Dict[str, Any], ev: Dict[str, Any]) -> None:
    ntype = str(ev.get("notification_type") or "")
    if ntype == "permission_prompt":
        if st.get("pending"):
            write_state(st)
            return  # the PermissionRequest hook already published this
        # Fallback: no PermissionRequest hook handled it — describe the dialog from the screen
        scr = screen_of(st["tmux"]) if tmux_has(st["tmux"]) else {"kind": "unknown"}
        pend = pending_from_screen(scr)
        if pend:
            st["pending"] = pend
            st["status"] = "awaiting_input" if pend["kind"] == "question" else "awaiting_permission"
            write_state(st)
            payload = build_payload(st, "ask", pend["kind"], pend)
            emit(payload)
            st.setdefault("notified", {})[pend["id"]] = payload["event_id"]
        write_state(st)
        return
    if ntype == "idle_prompt":
        # Stop normally already reported this turn; this is the safety net when it did not.
        if st["status"] in ("running", "starting"):
            _on_stop(st, {"last_assistant_message": None})
        else:
            write_state(st)
        return
    write_state(st)


# ----------------------------------------------------------------------------- doctor


def cmd_doctor(_a) -> None:
    ok = True

    def check(label: str, cond: bool, hint: str = "") -> None:
        nonlocal ok
        ok &= cond
        print(f"{'✓' if cond else '✗'} {label}" + (f" — {hint}" if not cond and hint else ""))

    print(f"ccb {VERSION} · config {CONFIG_PATH} · python {sys.version.split()[0]}")
    check("python ≥ 3.9", sys.version_info >= (3, 9))
    tv = tmux("-V", check=False, capture=True).stdout.strip()
    m = re.search(r"(\d+)\.(\d+)", tv)
    check(f"tmux ≥ 3.2 ({tv or 'missing'})", bool(m) and (int(m.group(1)), int(m.group(2))) >= (3, 2),
          "new-session -e needs tmux 3.2+")
    check("claude on PATH", shutil.which(CFG["claude_cmd"]) is not None)
    check("config present", CONFIG_PATH.exists(), f"missing {CONFIG_PATH}; run install.sh")
    check("sinks configured", bool(CFG["sinks"]), "config.sinks is empty")
    if "webhook" in CFG["sinks"]:
        check("webhook secret set", bool(CFG["webhook_secret"]), "CCB_WEBHOOK_SECRET / config.webhook_secret")
        try:
            with urllib.request.urlopen(f"{CFG['webhook_url']}/health", timeout=2) as r:
                check("hermes webhook /health", r.status == 200)
        except Exception as e:  # noqa: BLE001
            check("hermes webhook /health", False, f"{e}; WEBHOOK_ENABLED=true in ~/.hermes/.env and restart the gateway")
    if "command" in CFG["sinks"]:
        check("command_sink set", bool(CFG.get("command_sink")))
    settings = Path("~/.claude/settings.json").expanduser()
    data = _read_json(settings) if settings.exists() else None
    hooks = (data or {}).get("hooks", {})
    pr = json.dumps(hooks.get("PermissionRequest", []))
    check("PermissionRequest hook installed", "ccb.py hook" in pr, "run install.sh")
    to = [h.get("timeout", 600) for grp in hooks.get("PermissionRequest", []) for h in grp.get("hooks", [])
          if "ccb.py hook" in h.get("command", "")]
    check(f"PermissionRequest hook timeout > answer_timeout ({CFG['answer_timeout']}s)",
          bool(to) and max(to) > int(CFG["answer_timeout"]), "raise hook timeout or lower answer_timeout")
    for evname in ("Stop", "Notification", "SessionEnd", "UserPromptSubmit", "PostToolUse"):
        check(f"{evname} hook installed", "ccb.py hook" in json.dumps(hooks.get(evname, [])), "run install.sh")
    check(f"answer_timeout ≤ 3300 (hook timeout 3600 in template)", int(CFG["answer_timeout"]) <= 3300)
    check("stop_policy valid", CFG["stop_policy"] in ("notify", "markers", "silent"))
    sys.exit(0 if ok else 1)


# ----------------------------------------------------------------------------- main


def main() -> None:
    p = argparse.ArgumentParser(prog="ccb", description=__doc__.splitlines()[0])
    p.add_argument("--version", action="version", version=f"ccb {VERSION}")
    sp = p.add_subparsers(dest="cmd", required=True)

    s = sp.add_parser("start", help="start a Claude Code session in tmux")
    s.add_argument("name"); s.add_argument("--cwd", required=True); s.add_argument("--prompt")
    s.add_argument("--claude-args", metavar="ARGS", help="extra claude CLI flags as one quoted string, e.g. \"--model opus\"")
    s.add_argument("--json", action="store_true"); s.set_defaults(fn=cmd_start)

    s = sp.add_parser("answer", help="answer the pending question / permission of a session")
    s.add_argument("name"); s.add_argument("reply", nargs="*", help="option number(s), yes/always/no, or text")
    s.add_argument("--q", nargs=2, action="append", metavar=("N", "REPLY"), help="reply for question N (multi-question)")
    s.add_argument("--message", help="reason passed to Claude when denying")
    s.add_argument("--json", help="raw answer JSON (advanced)")
    s.add_argument("--release", action="store_true", help="let the terminal dialog stay for a human")
    s.set_defaults(fn=cmd_answer)

    s = sp.add_parser("send", help="type a new instruction into an idle session")
    s.add_argument("name"); s.add_argument("text"); s.add_argument("--raw", action="store_true"); s.set_defaults(fn=cmd_send)

    s = sp.add_parser("status", help="state of one or all sessions"); s.add_argument("name", nargs="?")
    s.add_argument("--json", action="store_true"); s.set_defaults(fn=cmd_status)
    s = sp.add_parser("list", help="one line per session"); s.set_defaults(fn=cmd_list)
    s = sp.add_parser("screen", help="parse what the terminal currently shows"); s.add_argument("name")
    s.add_argument("--json", action="store_true"); s.set_defaults(fn=cmd_screen)
    s = sp.add_parser("tail", help="raw last screen lines"); s.add_argument("name")
    s.add_argument("-n", type=int, default=40); s.set_defaults(fn=cmd_tail)
    s = sp.add_parser("stop", help="kill a session"); s.add_argument("name"); s.set_defaults(fn=cmd_stop)
    s = sp.add_parser("inbox", help="recent events written by the inbox sink"); s.add_argument("-n", type=int, default=20)
    s.set_defaults(fn=cmd_inbox)
    s = sp.add_parser("demo", help="start a smoke-test session that exercises every bridge path")
    s.add_argument("--name", default="demo"); s.add_argument("--cwd"); s.add_argument("--claude-args", metavar="ARGS")
    s.set_defaults(fn=cmd_demo)
    s = sp.add_parser("hook", help="Claude Code hook entry point (stdin JSON)"); s.set_defaults(fn=cmd_hook)
    s = sp.add_parser("doctor", help="check the installation"); s.set_defaults(fn=cmd_doctor)

    a = p.parse_args()
    a.fn(a)


if __name__ == "__main__":
    main()

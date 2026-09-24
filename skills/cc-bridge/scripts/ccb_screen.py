#!/usr/bin/env python3
"""ccb_screen — turn a Claude Code TUI screen (tmux capture-pane text) into structured state
and plan the keystrokes that answer a dialog.

Pure functions, stdlib only, Python 3.9+. This is the *fallback* path of the bridge: the
primary path answers dialogs through the PermissionRequest hook without touching the TUI.
The parser is used when the hook path is unavailable (hook timed out, session started
without hooks, human at the keyboard) and by `ccb screen` for diagnostics.

Recognised screens (fixtures in references/tui-dialogs.md, captured from Claude Code 2.1.281):

  trust            "Is this a project you created or one you trust?"  ❯ No, exit / Yes, I trust…
  permission       "<Tool> command … Do you want to proceed?"  1. Yes / 2. Yes, and always… / 3. No
  question         AskUserQuestion: tabs line (☐ Header … ✔ Submit), question, numbered options,
                   optional [ ]/[✔] checkboxes (multi-select), "Type something", "Submit", "Chat about this"
  question_review  "Review your answers … Ready to submit your answers?"  1. Submit answers / 2. Cancel
  dialog           any other numbered list with a ❯ cursor
  working          footer says "esc to interrupt"
  prompt           idle input prompt ("❯ " + "? for shortcuts")
  unknown          nothing matched

Every parse returns the same dict shape (see `parse_screen`).
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
RULE_RE = re.compile(r"^\s*[─━═—-]{6,}\s*$")
OPTION_RE = re.compile(
    r"^(?P<cursor>\s*❯)?\s*(?P<num>\d{1,2})\.\s+(?:(?P<check>\[[ xX✔✓]\])\s+)?(?P<label>\S.*?)\s*$"
)
CURSOR_ROW_RE = re.compile(r"^\s*❯\s+(?P<label>\S.*?)\s*$")
TAB_ITEM_RE = re.compile(r"([☐☒])\s+(.+?)(?=\s{2,}[☐☒✔←→]|\s*$)")
CHECK_TRUE = {"[x]", "[X]", "[✔]", "[✓]"}

FOOTER_MARKERS = (
    "Esc to cancel", "Enter to select", "Enter to confirm", "esc to interrupt",
    "? for shortcuts", "Tab to amend", "Tab/Arrow keys", "to navigate",
)
TRUST_MARKERS = ("Yes, I trust this folder", "Is this a project you created or one you trust")
PERMISSION_MARKER = "Do you want to proceed?"
REVIEW_MARKER = "Ready to submit your answers?"
SPECIAL_LABELS = (
    (re.compile(r"^Type something\.?$", re.I), "free_text"),
    (re.compile(r"^Chat about this\.?$", re.I), "chat"),
    (re.compile(r"^Submit$", re.I), "submit"),
)
KINDS_WITH_DIALOG = ("trust", "permission", "question", "question_review", "dialog")


def clean_lines(text: str) -> List[str]:
    return [ANSI_RE.sub("", ln).rstrip() for ln in (text or "").splitlines()]


def _is_footer(line: str) -> bool:
    return any(m in line for m in FOOTER_MARKERS)


def _is_tabs_line(line: str) -> bool:
    return ("☐" in line or "☒" in line) and not OPTION_RE.match(line)


def _special(label: str) -> Optional[str]:
    for rx, name in SPECIAL_LABELS:
        if rx.match(label.strip()):
            return name
    return None


def _empty(kind: str = "unknown") -> Dict[str, Any]:
    return {
        "kind": kind,
        "title": None,
        "question": None,
        "detail": [],
        "options": [],
        "cursor": None,
        "multi_select": False,
        "tabs": [],
        "footer": None,
        "answerable": False,
    }


def parse_screen(text: str) -> Dict[str, Any]:
    """Parse a pane capture into a screen description.

    Returns a dict with keys:
      kind, title, question, detail (list[str]), options (list[dict]), cursor (1-based row
      position of ❯ in `options`, or None), multi_select, tabs (list[{label, done}]),
      footer, answerable (bool: a dialog that keystrokes can answer).

    Each option: {"row": n (1-based position in the list, incl. special rows),
                  "index": digit shown or None, "label", "description", "checked": bool|None,
                  "cursor": bool, "special": None|"free_text"|"chat"|"submit"}.
    """
    lines = clean_lines(text)
    joined = "\n".join(lines)
    scr = _empty()

    # footer = last line that looks like a key hint
    footer_at = None
    for i in range(len(lines) - 1, -1, -1):
        if lines[i].strip() and _is_footer(lines[i]):
            footer_at = i
            break
    scr["footer"] = lines[footer_at].strip() if footer_at is not None else None
    body = lines[:footer_at] if footer_at is not None else lines

    if any(m in joined for m in TRUST_MARKERS):
        scr["kind"] = "trust"
    elif REVIEW_MARKER in joined:
        scr["kind"] = "question_review"
    elif PERMISSION_MARKER in joined:
        scr["kind"] = "permission"
    elif any(_is_tabs_line(ln) for ln in body):
        scr["kind"] = "question"
    else:
        scr["kind"] = None  # decided after option scan

    options, cursor_row = [], None
    for start in _dialog_region_starts(body, scr["kind"]):
        options, cursor_row = _scan_options(body[start:], scr["kind"])
        if cursor_row is not None or scr["kind"] is not None:
            break  # a dialog always has a ❯ row; for known kinds the first region is authoritative
    scr["options"] = options
    scr["cursor"] = cursor_row
    scr["multi_select"] = any(o["checked"] is not None for o in options)

    if scr["kind"] is None:
        footer = scr["footer"] or ""
        if options and any(o["cursor"] for o in options):
            scr["kind"] = "dialog"
        elif "esc to interrupt" in footer:
            scr["kind"] = "working"
        elif "? for shortcuts" in footer or _has_idle_prompt(body):
            scr["kind"] = "prompt"
        else:
            scr["kind"] = "unknown"

    kind = scr["kind"]
    if kind == "trust":
        scr["title"] = "Trust this folder?"
        scr["question"] = scr["title"]
    elif kind == "permission":
        _fill_permission(scr, body)
    elif kind == "question":
        _fill_question(scr, body)
    elif kind == "question_review":
        scr["title"] = "Review your answers"
        scr["question"] = REVIEW_MARKER
        scr["detail"] = [ln.strip() for ln in body
                         if ln.strip().startswith(("●", "→")) or ln.strip().startswith("→")]
    elif kind == "dialog":
        # best effort: the non-empty line right above the first option
        first = _first_option_line(body)
        if first is not None:
            for j in range(first - 1, -1, -1):
                if body[j].strip() and not RULE_RE.match(body[j]):
                    scr["question"] = body[j].strip()
                    break

    scr["answerable"] = kind in KINDS_WITH_DIALOG and bool(options)
    return scr


def _dialog_region_starts(body: List[str], kind: Optional[str]) -> List[int]:
    """Candidate indices where the current dialog starts, best first. Numbered lines above the
    dialog (e.g. the human's own prompt in the scrollback) must not be mistaken for options."""
    if kind in ("question", "question_review"):
        idx = [i for i, ln in enumerate(body) if _is_tabs_line(ln)]
        return [idx[-1]] if idx else [0]
    if kind == "permission":
        q_at = next((i for i, ln in enumerate(body) if PERMISSION_MARKER in ln), None)
        if q_at is not None:
            for j in range(q_at - 1, -1, -1):
                if RULE_RE.match(body[j]):
                    return [j + 1]
        return [0]
    if kind == "trust":
        return [0]
    # unknown kind: try after the last rule, then earlier rules, then the whole body — a dialog
    # may draw an inner rule (AskUserQuestion does, above "Chat about this")
    rules = [i + 1 for i, ln in enumerate(body) if RULE_RE.match(ln)]
    return list(reversed(rules)) + [0]


def _has_idle_prompt(body: List[str]) -> bool:
    for ln in reversed(body):
        if ln.strip():
            return ln.lstrip().startswith("❯")
    return False


def _first_option_line(body: List[str]) -> Optional[int]:
    for i, ln in enumerate(body):
        if OPTION_RE.match(ln):
            return i
    return None


def _scan_unnumbered_rows(body: List[str]):
    """Dialogs whose rows carry no digits (the trust prompt): from the first ❯ line to the end."""
    options: List[Dict[str, Any]] = []
    cursor_row = None
    started = False
    for ln in body:
        if not ln.strip() or RULE_RE.match(ln):
            continue
        cm = CURSOR_ROW_RE.match(ln)
        if not started and not cm:
            continue
        started = True
        label = cm.group("label").strip() if cm else ln.strip()
        opt = {"row": len(options) + 1, "index": None, "label": label, "description": "",
               "checked": None, "cursor": bool(cm), "special": _special(label)}
        options.append(opt)
        if opt["cursor"]:
            cursor_row = opt["row"]
    return options, cursor_row


def _scan_options(body: List[str], kind: Optional[str]):
    """Collect numbered options (and special unnumbered rows) with wrapped/continuation lines."""
    if kind == "trust":
        return _scan_unnumbered_rows(body)
    options: List[Dict[str, Any]] = []
    cursor_row = None
    in_list = False
    for ln in body:
        if not ln.strip():
            if in_list and options:
                # blank line inside a dialog list is fine; keep going
                continue
            continue
        if RULE_RE.match(ln):
            # the AskUserQuestion dialog draws a rule between "Type something" and "Chat about this"
            continue
        m = OPTION_RE.match(ln)
        if m:
            in_list = True
            label = m.group("label").strip()
            check = m.group("check")
            opt = {
                "row": len(options) + 1,
                "index": int(m.group("num")),
                "label": label,
                "description": "",
                "checked": (check in CHECK_TRUE) if check else None,
                "cursor": bool(m.group("cursor")),
                "special": _special(label),
            }
            options.append(opt)
            if opt["cursor"]:
                cursor_row = opt["row"]
            continue
        if in_list:
            cm = CURSOR_ROW_RE.match(ln)
            stripped = ln.strip()
            special = _special(stripped) or (_special(cm.group("label")) if cm else None)
            if special == "submit" or (cm and _special(cm.group("label"))):
                label = cm.group("label").strip() if cm else stripped
                opt = {"row": len(options) + 1, "index": None, "label": label, "description": "",
                       "checked": None, "cursor": bool(cm), "special": _special(label)}
                options.append(opt)
                if opt["cursor"]:
                    cursor_row = opt["row"]
                continue
            if _is_tabs_line(ln) or stripped.startswith("❯") and not cm:
                continue
            # continuation line: description (question dialogs) or wrapped label (others)
            if options:
                prev = options[-1]
                if kind == "question":
                    prev["description"] = (prev["description"] + " " + stripped).strip()
                else:
                    prev["label"] = (prev["label"] + " " + stripped).strip()
    return options, cursor_row


def _fill_permission(scr: Dict[str, Any], body: List[str]) -> None:
    q_at = None
    for i, ln in enumerate(body):
        if PERMISSION_MARKER in ln:
            q_at = i
            break
    scr["question"] = PERMISSION_MARKER
    if q_at is None:
        return
    # walk up to the previous rule; first non-empty line after it is the title
    start = 0
    for j in range(q_at - 1, -1, -1):
        if RULE_RE.match(body[j]):
            start = j + 1
            break
    block = [ln for ln in body[start:q_at] if ln.strip()]
    if block:
        scr["title"] = block[0].strip()
        scr["detail"] = [ln.strip() for ln in block[1:]]


def _fill_question(scr: Dict[str, Any], body: List[str]) -> None:
    tabs_at = None
    for i, ln in enumerate(body):
        if _is_tabs_line(ln):
            tabs_at = i  # keep the last tabs line (there is one per dialog)
    if tabs_at is None:
        return
    tabs = []
    for m in TAB_ITEM_RE.finditer(body[tabs_at]):
        tabs.append({"label": m.group(2).strip(), "done": m.group(1) == "☒"})
    scr["tabs"] = tabs
    first_opt = None
    for i in range(tabs_at + 1, len(body)):
        if OPTION_RE.match(body[i]):
            first_opt = i
            break
    q_lines = [ln.strip() for ln in body[tabs_at + 1:first_opt] if ln.strip()] if first_opt else []
    scr["question"] = " ".join(q_lines) if q_lines else None
    scr["title"] = tabs[0]["label"] if len(tabs) == 1 else None
    # tab with the active question: the first not-done tab (Submit is not listed)
    for t in tabs:
        if not t["done"]:
            scr["title"] = t["label"]
            break


# ----------------------------------------------------------------------------- keystrokes


def normalize_reply(reply: str) -> Dict[str, Any]:
    """Interpret a human/agent reply string.

    "2"        -> {"options": [2]}
    "1,3"      -> {"options": [1, 3]}
    "yes|y|да|approve|allow|ok" -> {"decision": "allow"}
    "always|всегда|don't ask" -> {"decision": "always"}
    "no|n|нет|deny|reject|cancel" -> {"decision": "deny"}
    anything else -> {"text": reply}
    """
    raw = (reply or "").strip()
    low = raw.lower()
    if re.fullmatch(r"\d{1,2}(\s*[,\s]\s*\d{1,2})*", raw):
        nums = [int(x) for x in re.split(r"[,\s]+", raw) if x]
        return {"options": nums}
    if low in {"y", "yes", "да", "д", "approve", "allow", "ok", "ок", "accept", "разреши", "разрешить"}:
        return {"decision": "allow"}
    if low in {"always", "всегда", "don't ask", "dont ask", "allow always", "always allow"}:
        return {"decision": "always"}
    if low in {"n", "no", "нет", "deny", "reject", "cancel", "отмена", "запрети", "запретить", "отказ"}:
        return {"decision": "deny"}
    return {"text": raw}


def _find_option(options: List[Dict[str, Any]], predicate) -> Optional[Dict[str, Any]]:
    for o in options:
        if predicate(o):
            return o
    return None


def plan_keys(screen: Dict[str, Any], reply: Dict[str, Any]) -> List[Dict[str, str]]:
    """Return the key sequence that applies `reply` to the parsed `screen`.

    Tokens: {"key": "Enter"|"Down"|"Up"|"Escape"|"<digit>"} or {"text": "..."}.
    Raises ValueError when the reply cannot be applied to this screen.
    Multi-question dialogs need one plan per question screen; `question_review` is a
    separate screen answered with option 1.
    """
    kind = screen.get("kind")
    options = screen.get("options") or []
    if kind not in KINDS_WITH_DIALOG or not options:
        raise ValueError(f"screen is not an answerable dialog (kind={kind})")

    if kind == "trust":
        target = _find_option(options, lambda o: "trust" in o["label"].lower())
        if reply.get("decision") == "deny":
            target = _find_option(options, lambda o: o["label"].lower().startswith("no"))
        return _navigate_and_enter(screen, target)

    if kind in ("permission", "dialog", "question_review"):
        decision = reply.get("decision")
        target = None
        if reply.get("options"):
            target = _find_option(options, lambda o: o["index"] == reply["options"][0])
        elif decision == "allow":
            target = _find_option(options, lambda o: o["label"].lower().startswith("yes") and "always" not in o["label"].lower()
                                  and "don't ask" not in o["label"].lower()) or _find_option(options, lambda o: o["index"] == 1)
        elif decision == "always":
            target = _find_option(options, lambda o: "always" in o["label"].lower() or "don't ask" in o["label"].lower()) \
                or _find_option(options, lambda o: o["label"].lower().startswith("yes"))
        elif decision == "deny":
            target = _find_option(options, lambda o: o["label"].lower().startswith("no") or o["label"].lower() == "cancel")
        elif kind == "question_review" and not reply:
            target = _find_option(options, lambda o: o["index"] == 1)
        if target is None:
            raise ValueError("cannot map reply to a permission option; use a number 1..%d" % len(options))
        return _navigate_and_enter(screen, target)

    # AskUserQuestion
    keys: List[Dict[str, str]] = []
    if reply.get("text") is not None and not reply.get("options"):
        free = _find_option(options, lambda o: o["special"] == "free_text")
        if free is None:
            raise ValueError("this question has no free-text row")
        keys += _navigate_to(screen, free)
        keys.append({"text": reply["text"]})
        keys.append({"key": "Enter"})
        return keys
    wanted = reply.get("options") or []
    if reply.get("decision") == "allow" and not wanted:
        wanted = [1]
    if not wanted:
        raise ValueError("reply has neither options nor text")
    numbered = {o["index"]: o for o in options if o["index"] is not None and o["special"] is None}
    missing = [n for n in wanted if n not in numbered]
    if missing:
        raise ValueError(f"option(s) {missing} not on screen; available: {sorted(numbered)}")
    if screen.get("multi_select"):
        for n in wanted:
            if numbered[n]["checked"] is not True:
                keys.append({"key": str(n)})  # digit toggles the checkbox
        submit = _find_option(options, lambda o: o["special"] == "submit")
        if submit is None:
            raise ValueError("multi-select dialog without a Submit row")
        # after toggling, the cursor sits on the last toggled row; recompute from there
        last_row = numbered[wanted[-1]]["row"] if any(numbered[n]["checked"] is not True for n in wanted) else screen.get("cursor") or 1
        keys += _moves(last_row, submit["row"])
        keys.append({"key": "Enter"})
        return keys
    # single select: the digit selects and advances
    keys.append({"key": str(wanted[0])})
    return keys


def _moves(from_row: int, to_row: int) -> List[Dict[str, str]]:
    delta = to_row - from_row
    key = "Down" if delta > 0 else "Up"
    return [{"key": key} for _ in range(abs(delta))]


def _navigate_to(screen: Dict[str, Any], target: Optional[Dict[str, Any]]) -> List[Dict[str, str]]:
    if target is None:
        raise ValueError("target option not found")
    cur = screen.get("cursor") or 1
    return _moves(cur, target["row"])


def _navigate_and_enter(screen: Dict[str, Any], target: Optional[Dict[str, Any]]) -> List[Dict[str, str]]:
    keys = _navigate_to(screen, target)
    keys.append({"key": "Enter"})
    return keys


# ----------------------------------------------------------------------------- rendering


def describe(screen: Dict[str, Any]) -> str:
    """One-paragraph human summary of a parsed screen (used by `ccb screen` and fallbacks)."""
    kind = screen.get("kind")
    if kind == "permission":
        head = screen.get("title") or "Permission"
        det = " ".join(screen.get("detail") or [])
        opts = "; ".join(f"{o['index']}. {o['label']}" for o in screen["options"] if o["index"])
        return f"{head}: {det}\n{PERMISSION_MARKER} {opts}"
    if kind == "question":
        q = screen.get("question") or "(question)"
        opts = "; ".join(f"{o['index']}. {o['label']}" for o in screen["options"] if o["index"] and not o["special"])
        ms = " (multi-select)" if screen.get("multi_select") else ""
        return f"{q}{ms}\n{opts}"
    if kind == "question_review":
        return "Review your answers: " + " | ".join(screen.get("detail") or [])
    if kind == "trust":
        return "Claude Code asks whether to trust the working folder."
    if kind == "working":
        return "Claude is working."
    if kind == "prompt":
        return "Claude is idle at the input prompt."
    return f"Screen kind: {kind}"

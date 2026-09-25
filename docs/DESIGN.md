# Design: Claude Code ↔ Hermes bridge

_Last updated: 2026-09-24. Single source of truth for what we are building and why. Update it
when a decision changes. Verified facts are marked **[verified 2.1.281]** — they were tested
against Claude Code 2.1.281 and Hermes Agent v0.21.0 on 2026-09-24._

## 1. Problem

- A headless machine runs a **Hermes Agent** with a chat gateway (Telegram here; any Hermes
  platform works). The owner talks to the machine only through that chat.
- Development is delegated to **Claude Code** sessions, one per task, each in its own
  **tmux** session.
- The owner wants to be pinged **only** when a session is blocked on a human (a question, a
  permission) or has finished a turn — and wants to answer from the chat, with the answer
  landing in the session as if typed at the keyboard.
- Hard constraint: **every notification and every reply goes through Hermes.** Scripts and
  hooks never talk to the chat platform directly. Hermes formats what the human sees and
  relays what the human says.
- The bridge is a **generic, reusable component**: it must not know anything about the coding
  workflow (skills such as `task-flow`), must install on any Hermes instance with one command,
  and must behave predictably enough that users layer their own context on top.

### What was tried and why it failed

| Iteration | Mechanism | Problem |
|---|---|---|
| 1 | Hermes cron jobs polling `tmux capture-pane` | Polling burns LLM turns, screen scraping is brittle, one cron per task. |
| 2 | Claude Code hooks writing event files, still read by cron | Still polling; hooks half-worked (different `$HOME`/PATH inside tmux, `Stop` firing every turn without dedup). |
| 3 (this) | Hooks push events; the `PermissionRequest` hook **blocks** and returns the human's answer as a decision; a deterministic CLI owns tmux and state | — |

## 2. Principles

1. **Deterministic first.** Everything that can be a script is a script. The LLM does exactly
   two things: phrase a question to the human, interpret the human's free-text reply. Events
   that need no phrasing bypass the LLM (`deliver_only`).
2. **Push, not poll.** Claude Code hooks → sink. No cron, no screen polling.
3. **Answer through the hook, not the keyboard.** Claude Code's `PermissionRequest` hook can
   return the answer to an `AskUserQuestion` and the decision for a permission
   **[verified 2.1.281]**. Keystrokes into the TUI are the fallback, never the primary path.
4. **One CLI owns tmux.** Hermes runs `ccb …`, never raw `tmux`/`claude`.
5. **State on disk.** `~/.cache/ccb/sessions/<name>.json` is the truth about a session; any
   Hermes turn can read it; nothing depends on chat history.
6. **Transport-agnostic events.** One payload contract (`references/event-contract.md`) for
   every sink: Hermes webhook today, the Hermes plugin (inline buttons) tomorrow, a user's own
   script any time.
7. **Installable from GitHub.** `hermes skills install …` + an idempotent `install.sh` for what
   the skill installer cannot touch.

## 3. Architecture

```
┌────────────────────────── tmux session ccb-<name> ───────────────────────────┐
│ claude   (env CCB_TASK=<name>)                                                │
│   PermissionRequest ─► ccb hook ─► pending + `ask` event ─► WAIT ◄─ answer file│
│                                    ◄─ decision JSON (allow+answers / deny)    │
│   Stop / Notification / SessionEnd / PostToolUse / UserPromptSubmit ─► ccb hook│
└────────────────────────────────────────┬──────────────────────────────────────┘
                                         │ sinks (config.sinks)
              ┌──────────────────────────┼───────────────────────────┐
              ▼                          ▼                           ▼
   webhook: POST 127.0.0.1:8644   inbox: ~/.cache/ccb/inbox/*.json   command: your script
   /webhooks/ccb-ask|ccb-notify   (Hermes plugin v1 consumes)        (anything else)
              │
   Hermes webhook adapter: script ccb-filter.py → deliver_only (notify) │ agent + skill (ask)
              ▼
   human answers in the chat → Telegram-session agent (skill cc-bridge) → `ccb answer <name> …`
                                                                        → answer file → hook returns
```

### 3.1 `ccb` CLI (`skills/cc-bridge/scripts/ccb.py`, parser in `ccb_screen.py`)

Stdlib only, Python 3.9+ (hooks run the system `python3` of the shell inside tmux).

| Command | Effect |
|---|---|
| `ccb start <name> --cwd DIR [--prompt TEXT] [--claude-args "…"]` | `tmux new-session -d -e CCB_TASK=<name>`, launches `claude`, answers the trust prompt via the parser, waits for the idle prompt, sends `--prompt`. |
| `ccb answer <name> <reply…> [--q N REPLY]… [--message M] [--release]` | Validates the reply against `pending`, writes the answer file; the blocked hook returns the decision. Without a live hook: drives the on-screen dialog with keystrokes. |
| `ccb send <name> TEXT [--raw]` | Types a new instruction into an idle session (refuses while a dialog is open). |
| `ccb status [name] [--json]`, `ccb list` | State files, including what is pending. |
| `ccb screen <name> [--json]`, `ccb tail <name>` | Parsed / raw view of the terminal. |
| `ccb stop <name>` | Marks `stopped_by_ccb` (suppresses the `stopped` notify), kills the session. |
| `ccb inbox` | Recent events written by the inbox sink. |
| `ccb demo` | Smoke-test session (`references/test-prompt.md`). |
| `ccb hook` | Hook entry point (stdin JSON). No-op unless `CCB_TASK` is set. |
| `ccb doctor` | Versions, config, sinks, every hook entry and its timeout. |

Config `~/.config/ccb/config.json` — `sinks`, `webhook_url`, `webhook_secret`,
`webhook_routes`, `command_sink`, `stop_policy` (`notify|markers|silent`), `answer_timeout`
(3300), `claude_args`, `tail_lines`, `message_limit`, `raw_log`.

### 3.2 State file (`~/.cache/ccb/sessions/<name>.json`, schema 2)

```json
{
  "schema": 2, "name": "kd-123", "tmux": "ccb-kd-123", "cwd": "/…/repo",
  "claude_session_id": "…", "transcript_path": "…/.jsonl",
  "status": "starting | running | awaiting_input | awaiting_permission | idle | done | stopped | error",
  "pending": {
    "id": "req-1790200962652-9f8e7d", "kind": "question | permission", "source": "hook | screen",
    "hook_pid": 4242, "tool_name": "AskUserQuestion", "tool_input": {…},
    "questions": [ {"index": 1, "question": "…", "header": "…", "multi_select": false,
                    "options": [{"index": 1, "label": "…", "description": "…"}], "allow_free_text": true} ],
    "permission": {"tool_name": "Bash", "summary": "…", "input": {…}, "suggestions": […], "choices": […]},
    "created_at": "…"
  },
  "last_message": "last assistant text, marker stripped, ≤ 4000 chars",
  "marker": "NEED_INPUT | DONE | ROUND:3 | null",
  "last_event": "PermissionRequest", "last_event_at": "…", "started_at": "…", "updated_at": "…",
  "stopped_by_ccb": false,
  "notified": {"req-…": "event_id", "stop-<sha1>": "event_id"},
  "last_answer": {"request_id": "…", "decision": "allow", "answers": {…}, "at": "…"}
}
```

Written atomically (tmp + rename). `pending` is the contract between the blocked hook and
`ccb answer`; `notified` is the dedup guard.

### 3.3 The interception, in detail **[verified 2.1.281]**

1. Claude calls `AskUserQuestion` (or a tool needing permission). Claude Code runs the
   `PermissionRequest` hook **and draws the dialog at the same time**.
2. `ccb hook` builds `pending`, writes state, emits `ask` to every sink. If **no** sink
   accepted the event it returns immediately (the dialog is already on screen; nobody would
   answer remotely anyway).
3. It then polls `~/.cache/ccb/answers/<name>/<request_id>.json` every 0.5 s and re-reads the
   state file every 2 s, until:
   - the answer file appears → returns
     `{"decision": {"behavior": "allow", "updatedInput": {…, "answers": {question: value}}}}`
     for questions, `allow` (+`updatedPermissions` echoing `permission_suggestions` for
     "always") or `deny` (+`message`) for permissions. The dialog closes; Claude Code prints
     "Allowed/Denied by PermissionRequest hook".
   - `pending` disappears (the human answered in the terminal → `PostToolUse`, or a new
     prompt → `UserPromptSubmit`, or `ccb stop`) → returns nothing.
   - `answer_timeout` (3300 s < hook timeout 3600 s) → returns nothing; the dialog stays.
   - `ccb answer --release` → returns nothing on purpose.
4. Multi-select answers are option labels joined with `", "`; free text is passed as typed.
   This is exactly what the TUI itself produces in `tool_response.answers`.

Why not `PreToolUse`? It also fires for `AskUserQuestion`, but it can only allow/deny, not
supply answers; a deny-with-reason would make Claude re-ask. `PermissionRequest` is the event
Claude Code itself uses to collect the answer.

### 3.4 Fallback: the TUI parser (`ccb_screen.py`)

Used when no hook is waiting: sessions started before the hooks were installed, hook timeout,
`--release`, or a `Notification/permission_prompt` arriving with no `pending`. It recognises
the trust prompt, permission dialogs, single/multi-select `AskUserQuestion` screens
(including the tabs line for several questions and the review screen), the idle prompt and
the working state, and plans keystrokes: digits select/toggle options, Down/Up + Enter for
unnumbered rows (trust, Submit), typed text + Enter for free text. Fixtures from real screens
live in `tests/fixtures_screens.py` and `references/tui-dialogs.md`. Numbered lines above the
dialog (the human's prompt in the scrollback) are excluded by anchoring on the dialog's tabs
line or rule.

### 3.5 Stop handling and markers

`Stop` carries `last_assistant_message`. `stop_policy: notify` (default) sends every finished
turn as `notify/idle` — the predictable behaviour for a generic bridge: the human sees what
Claude said and can `ccb send` the next instruction. `markers` is for users whose coding skill
ends each turn with `[[CCB:ROUND:n]]` / `[[CCB:NEED_INPUT]]` / `[[CCB:DONE]]` and who want
intermediate rounds silent. `silent` only relays `ask` events. Markers are always parsed and
stripped; nothing in the bridge depends on them.

### 3.6 Hermes side (webhook sink)

Routes (`templates/webhook-routes.yaml`, created by `install.sh`):

- `ccb-notify` — `deliver_only`, `prompt: "{text}"`. Zero tokens, sub-second.
- `ccb-ask` — agent run with `skills: [cc-bridge]`, `toolsets: [terminal]`, prompt carrying
  `text`, `kind`, `request_id`, `reply_hint`, `answer_command`. The agent rephrases the
  question in the human's language and stops. Can be switched to `deliver_only` too.
- Both pass through `script: ccb-filter.py` (stale/duplicate `ask` → `[SILENT]`).
- Auth: HMAC v2 with the shared `CCB_WEBHOOK_SECRET`.

**Reply routing.** The human's reply arrives in the *chat* session, not the webhook session.
The skill's rule (procedure C): one `pending` → the message is the reply → `ccb answer`;
several → ask which; none → `ccb send` if the session is idle.

**Why no inline buttons yet.** Hermes has a `clarify` tool that renders Telegram inline buttons,
but only inside a *chat-session* agent run: in a webhook-triggered run the status adapter is
the webhook adapter (plain numbered text) and the button/text intercept is keyed to the
webhook session, so the human's tap never reaches it **[verified in gateway/run.py and
tools/clarify_gateway.py]**. Buttons therefore need the plugin (§4).

## 4. Skill now, plugin next

| | Skill (v0.2, this repo) | Plugin (v1.0) |
|---|---|---|
| Transport | webhook sink → Hermes webhook adapter | inbox sink → plugin poller inside the gateway (no `WEBHOOK_ENABLED`, no HMAC, no filter script) |
| Ask rendering | agent turn (or deliver_only text) | Telegram inline buttons (`register_telegram_handler`, callbacks `ccb:<request_id>:<q>:<opt>`), zero LLM |
| Answer | human text → chat agent → `ccb answer` | button tap → `ccb answer` directly; free text still via the chat agent |
| Tools for the model | `terminal` (`ccb …`) | typed `cc_start / cc_answer / cc_send / cc_status` |
| Slash command | `/cc-bridge` (automatic for every skill) | `/ccb start|status|answer|stop` via `register_command` |
| Install | `hermes skills install owner/repo/skills/cc-bridge` + `install.sh` | `hermes plugins install owner/repo` (+ `hermes plugins enable`) |

The plugin reuses `ccb.py` unchanged: it consumes `~/.cache/ccb/inbox/`, renders buttons,
and shells out to `ccb answer`. The skill stays the documented behaviour for both.

## 5. Distribution

- Repo is a Hermes **tap**: skills under `skills/<name>/SKILL.md`; `skills.sh.json` groups them.
- Hermes copies `SKILL.md` **plus only the support files SKILL.md references** under
  `scripts/`, `templates/`, `references/`. Every shipped file must be mentioned in SKILL.md
  (checked in `tests/test_packaging.py`).
- `install.sh` handles `~/.hermes/scripts/`, webhook routes, `~/.claude/settings.json`, PATH;
  `--dry-run` shows the plan; `--no-hermes` for inbox/command-only setups.
- Trust level `community`: loopback-only network, obvious code.

## 6. Decisions log

| Date | Decision | Why |
|---|---|---|
| 2026-09-24 | Interception through `PermissionRequest` returning `updatedInput.answers` | Verified: closes the dialog, no keystrokes, exact TUI semantics. |
| 2026-09-24 | Block the hook while waiting | Verified: the dialog is drawn anyway, so a local human is never locked out. |
| 2026-09-24 | `stop_policy: notify` default | Generic bridge: every finished turn is worth one message; markers are opt-in. |
| 2026-09-24 | Sinks abstraction (`webhook` / `inbox` / `command`) | Same payload for webhook today, plugin tomorrow, user scripts any time. |
| 2026-09-24 | Buttons deferred to the plugin | `clarify` cannot reach the chat session from a webhook run. |
| 2026-09-24 | `raw_log` off by default | Hook payloads contain tool inputs; opt in for debugging. |

## 7. Open questions

- [ ] `answer_timeout` 55 min — long enough for a human, short enough not to hold a `claude`
      process forever? Tune with real use.
- [ ] Should `notify/idle` include the *whole* last message or the first N lines? Currently
      1500 chars.
- [ ] Hermes `webhook_subscriptions.json` edit for `toolsets` — is hot-reload of that key
      confirmed in the running gateway? (Docs say the file is mtime-reloaded.)
- [ ] tmux < 3.2 wrapper in `ccb start` (currently documented, not implemented).

## 8. Roadmap and task breakdown

**v0.2 — skill + hook interception (this repo, done):** `ccb` with `answer`/`screen`/`demo`,
sinks, filter, templates, install.sh, tests (37), references.

**v0.3 — first real deployment (done 2026-09-25 on the owner's machine; routes, hooks and a Telegram-driven `ccb demo` verified):**
1. Enable the Hermes webhook adapter (`platforms.webhook.enabled: true` in config.yaml, gateway restart), run
   `install.sh`, `ccb doctor`.
2. Remove the iteration-2 hooks (`cc_hook.py`) from `~/.claude/settings.json`.
3. `ccb demo` through Telegram; fix whatever the agent gets wrong in procedure B/C.
4. Decide `ccb-ask` mode: agent turn vs `deliver_only` text.

**v1.0 — Hermes plugin `cc-bridge` (specification: `docs/tasks/v1.0-plugin.md`):**
5. Plugin skeleton (`plugin.yaml`, `register(ctx)`), bundled skill via `ctx.register_skill`.
6. Inbox poller task started from the Telegram handler factory; renders `ask` with inline
   buttons and `notify` as text; resolves home chat id from the adapter config.
7. `CallbackQueryHandler(pattern=r"^ccb:")` → `ccb answer`; "Other" button → text capture
   through the existing chat agent (skill procedure C).
8. Typed tools `cc_start/cc_answer/cc_send/cc_status` (thin wrappers over `ccb --json`).
9. `/ccb` slash command.
10. `hermes plugins doctor --ci` in CI; install docs for both paths.

**Later:** per-session chat threads (forum topics), `deliver_extra.chat_id` per session,
Discord/Slack button renderers via `register_platform_handler`, Windows/ConPTY (explicitly out
of scope for now).

## 9. Non-goals

- Replacing Claude Code's permission model or running `--dangerously-skip-permissions`.
- Streaming Claude's full output to the chat. Only blocking states and finished turns.
- Non-tmux transports. tmux is the contract.
- Knowing anything about the coding task (branches, tickets, review rounds).

## 10. Glossary

- **Session name** — short slug like `kd-123`; tmux session `ccb-<name>`.
- **Pending / request** — the question or permission Claude is blocked on; `request_id`.
- **Sink** — where events go: `webhook`, `inbox`, `command`.
- **Route** — a named Hermes webhook endpoint `/webhooks/<route>`.
- **Marker** — optional `[[CCB:…]]` last-line token emitted by a coding skill.

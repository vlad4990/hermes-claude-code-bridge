# Design: Claude Code ↔ Hermes bridge

_Last updated: 2026-09-23. This document is the single source of truth for what we are
building and why. Update it when a decision changes._

## 1. Problem

The setup this grew out of:

- A headless machine (Mac mini, Intel, macOS) runs a **Hermes Agent** with the Telegram
  gateway. The owner talks to the machine only through Telegram.
- Development work is delegated to **Claude Code** sessions, one per Jira task, each in its
  own **tmux** session (`task-*`, `fix-*`, `pr-*`, later one per `KD-xxx`). Claude Code
  follows a `task-flow` skill: implement → self-review rounds → ready to merge.
- The owner wants to be pinged **only** when
  1. a Claude Code session is blocked waiting for human input (question, permission), or
  2. a task has finished all review rounds and is ready to merge.
  Intermediate review rounds must stay silent.
- Hard constraint: **every notification and every reply goes through Hermes.** Scripts and
  hooks never talk to Telegram directly. Hermes formats what the human sees and relays
  what the human says back into the terminal.

### What was tried and why it failed

| Iteration | Mechanism | Problem |
|---|---|---|
| 1 | Hermes cron jobs (`monitor-kd*`, one per task) + `claude_session_status.py` scraping `tmux capture-pane` | Polling. Wasted LLM turns every tick, screen scraping brittle, per-task cron sprawl. |
| 2 | Claude Code hooks (`~/.hermes/scripts/cc_hook.py` → event files in `~/.cache/cc-events/`) still read by the same cron jobs via `cc_event.sh` | Still polling; hooks half-worked; ended with neither cron nor hook path functioning. |
| 3 (this) | Hooks push directly into **Hermes' webhook adapter**; deterministic CLI owns tmux and state; skill tells Hermes what to do with the event | — |

Root causes we believe killed iteration 2 (see `references/troubleshooting.md`):
hooks only load at Claude Code session start; `Stop` fires every assistant turn without
dedup; hook scripts ran under a different `$HOME`/PATH inside tmux; no single place to
test the pipeline end to end.

## 2. Principles

1. **Deterministic first.** If a step can be a script, it is a script. The LLM is used
   for exactly two things: phrasing a question to the human, and interpreting the human's
   free-text reply. Notifications that need no phrasing bypass the LLM entirely.
2. **Push, not poll.** No cron. Claude Code hooks → HTTP POST → Hermes webhook adapter.
3. **One CLI owns tmux.** Hermes never runs raw `tmux` commands; it runs `ccb`. This keeps
   the skill short and makes the model's job "call a command" not "remember tmux flags".
4. **State on disk, not in the model's head.** `~/.cache/ccb/sessions/<name>.json` is the
   truth about every session. Any Hermes turn can read it; nothing depends on chat history.
5. **Everything installable from GitHub.** `hermes skills install` gets the skill;
   `install.sh` (run by the agent or human, idempotent) gets the parts Hermes' installer
   does not touch (webhook routes, `~/.hermes/scripts/`, `~/.claude/settings.json`).

## 3. Architecture

```
┌──────────────────────── tmux session ccb-<task> ────────────────────────┐
│ claude  (env CCB_TASK=<task>)                                            │
│   hooks: Stop / Notification / PermissionRequest / SessionEnd            │
│       └─► python3 ccb.py hook   (stdin: hook JSON)                       │
└──────────────────────────────────────┬───────────────────────────────────┘
                                       │ 1. update ~/.cache/ccb/sessions/<task>.json
                                       │ 2. choose route: notify | ask | silent
                                       │ 3. POST http://127.0.0.1:8644/webhooks/ccb-<route>
                                       │    (HMAC-SHA256 v2, X-Request-ID = event id)
                                       ▼
┌──────────────────── Hermes gateway: webhook adapter ─────────────────────┐
│ route ccb-notify: script ccb-filter.py → deliver_only → Telegram         │
│ route ccb-ask:    script ccb-filter.py → agent(skill cc-bridge) → Telegram│
└──────────────────────────────────────┬───────────────────────────────────┘
                                       ▼
                             human answers in Telegram
                                       ▼
┌──────────────── Hermes (Telegram session, skill cc-bridge) ──────────────┐
│ ccb status  → which session is awaiting_input?                           │
│ ccb send <task> "<answer>"  → tmux send-keys … ; Enter                   │
└──────────────────────────────────────────────────────────────────────────┘
```

### 3.1 `ccb` CLI (`skills/cc-bridge/scripts/ccb.py`)

Stdlib only. Subcommands:

| Command | Effect |
|---|---|
| `ccb start <name> --cwd DIR [--prompt TEXT] [--claude-args …]` | `tmux new-session -d -s ccb-<name> -c DIR -e CCB_TASK=<name>`, launches `claude`, waits for the prompt, sends `--prompt` if given, writes state `running`. |
| `ccb send <name> TEXT` | `tmux send-keys -l TEXT`, short pause, separate `Enter`. State → `running`, clears `question`. |
| `ccb status [name] [--json]` | Prints one or all state files. |
| `ccb tail <name> [-n 40]` | `tmux capture-pane -p` last N lines. |
| `ccb stop <name>` | `tmux kill-session`, state → `stopped`. |
| `ccb list` | Sessions with status, age, one-line summary. |
| `ccb hook` | Hook entry point. Reads Claude Code hook JSON on stdin. No-op unless `CCB_TASK` is set (so manual `claude` sessions are untouched). |
| `ccb doctor` | Checks tmux, claude, webhook `/health`, routes, hooks block, config. |

Config: `~/.config/ccb/config.json` — `webhook_url` (default `http://127.0.0.1:8644`),
`webhook_secret`, `state_dir`, `tmux_prefix`, `claude_args`, `raw_log` (bool: dump raw hook
payloads to `~/.cache/ccb/raw/` for debugging).

### 3.2 State file (`~/.cache/ccb/sessions/<name>.json`)

```json
{
  "name": "kd-123",
  "tmux": "ccb-kd-123",
  "cwd": "/Users/hermes/work/repo",
  "claude_session_id": "…",
  "transcript_path": "…/.jsonl",
  "status": "running | awaiting_input | awaiting_permission | done | stopped | error",
  "question": "text Claude is waiting on (null when running)",
  "permission": {"tool_name": "Bash", "tool_input": {...}} ,
  "last_message": "last assistant message, truncated to 2000 chars",
  "marker": "NEED_INPUT | DONE | ROUND:3 | null",
  "last_event": "Stop",
  "last_event_at": "2026-09-23T10:00:00Z",
  "started_at": "…",
  "updated_at": "…",
  "notified": {"ask": "<event id>", "notify": "<event id>"}
}
```

Written atomically (tmp + rename). `notified` is the dedup guard so the same blocking
state is never announced twice.

### 3.3 Event → route mapping (deterministic, lives in `ccb hook`)

| Hook event | Condition | State | Route |
|---|---|---|---|
| `PermissionRequest` | always | `awaiting_permission` | `ccb-ask` |
| `Notification` | type is permission/idle prompt | `awaiting_input` | `ccb-ask` (if not already notified) |
| `Stop` | `stop_hook_active` true | ignore | silent |
| `Stop` | last message carries `[[CCB:DONE]]` | `done` | `ccb-notify` |
| `Stop` | last message carries `[[CCB:ROUND:n]]` | `running` | silent |
| `Stop` | last message carries `[[CCB:NEED_INPUT]]` or ends with a question | `awaiting_input` | `ccb-ask` |
| `Stop` | otherwise | `awaiting_input` | `ccb-ask` after a debounce (default 20 s) unless a new `UserPromptSubmit`/send arrives |
| `SessionEnd` | any | `stopped` | `ccb-notify` |

**Markers.** The cleanest way to avoid LLM-based classification is to make Claude Code
say what state it is in. The coding skill (`task-flow`) should end each turn with exactly
one machine-readable last line:

```
[[CCB:ROUND:2]]      # finished an intermediate review round, continuing
[[CCB:NEED_INPUT]]   # blocked on a human decision — the question is above
[[CCB:DONE]]         # all rounds done, ready to merge
```

`ccb hook` reads the last assistant message from `transcript_path`, strips the marker,
and never has to guess. The fallback heuristics above exist only for sessions without
markers.

### 3.4 Hermes side

**Webhook routes** (`templates/webhook-routes.yaml`, created by `install.sh` via
`hermes webhook subscribe` so no `config.yaml` edits and no gateway restart):

- `ccb-notify` — `deliver_only: true`, `deliver: telegram`, prompt template renders the
  message from payload fields. Zero tokens.
- `ccb-ask` — `skills: ["cc-bridge"]`, `toolsets: ["terminal", "file"]`, `deliver: telegram`.
  Prompt gives the agent `name`, `question`, `permission`, last 30 lines of `tail`, and
  the instruction to ask the human concisely.

Both routes use `script: ccb-filter.py` (must live under `~/.hermes/scripts/`) as a last
deterministic guard: rate-limit per session, drop if state changed since the event was
emitted, `[SILENT]` otherwise.

Auth: HMAC v2 (`X-Webhook-Signature-V2` + `X-Webhook-Timestamp`). Secret shared via
`CCB_WEBHOOK_SECRET` declared in the skill's `required_environment_variables`, so Hermes
prompts for it once and passes it through to the terminal tool.

**Skill `cc-bridge`** — see `skills/cc-bridge/SKILL.md`. Responsibilities:
start a task on request; on a `ccb-ask` event read state + tail and ask the human; on a
human reply find the session in `awaiting_*` and `ccb send`; never touch tmux directly;
never announce intermediate rounds.

### 3.5 The reply-routing problem

The human's answer arrives in the **Telegram** session, not the session the webhook
spawned. We do not rely on shared context between them. The skill's rule:

> If exactly one session is `awaiting_input`/`awaiting_permission`, treat a reply that is
> not a new command as the answer and `ccb send` it. If several are waiting, ask which.
> If none are waiting, it is a normal message.

For permissions specifically, `ccb send` maps `yes/да/approve` → `y`, `no/нет/deny` → `n`
(and the raw text otherwise) so the agent does not need to know Claude Code's prompt UI.

## 4. Why a skill and not a plugin (yet)

| | Skill (v0.x) | Plugin (v1) |
|---|---|---|
| Effort | one markdown file + scripts | Python package, `register(ctx)`, testing |
| Tools exposed to the model | via `terminal` (`ccb …`) | typed `cc_start/cc_send/cc_status` schemas |
| Slash command | `/cc-bridge` (automatic for every skill) | `/ccb` with custom handler |
| Telegram inline buttons (Approve / Deny / Tail) | no | yes — `ctx.register_platform_handler("telegram", …)` with pattern-scoped callbacks |
| Listed in system-prompt skill index | yes | plugin-bundled skills are explicit-load only |
| Install | `hermes skills install owner/repo/skills/cc-bridge` | `hermes plugins install owner/repo` |

Decision: ship the skill first, keep `ccb` as the stable core, add the plugin on top
without changing `ccb`.

## 5. Distribution

- Repo is a Hermes **tap**: skills under `skills/<name>/SKILL.md`. Users run
  `hermes skills install <owner>/hermes-claude-code-bridge/skills/cc-bridge` or add the tap.
- Hermes copies `SKILL.md` **plus only the support files SKILL.md references** under
  `scripts/`, `templates/`, `references/`, `assets/`, `examples/`. Every shipped file must
  therefore be mentioned in SKILL.md.
- `install.sh` handles what the skill installer cannot: `~/.hermes/scripts/`, webhook
  routes, `~/.claude/settings.json`, PATH. Idempotent; the skill's Setup section tells the
  agent to run it when `ccb doctor` fails.
- Updates: `hermes skills check` / `hermes skills update`. Locally edited copies are
  skipped unless `--force`.
- Trust level `community`: scripts pass the security scanner; a `dangerous` verdict cannot
  be forced. Keep network calls loopback-only and obvious.

## 6. Open questions

- [ ] Exact field names of current Claude Code hook payloads (`Notification.notification_type`,
      `PermissionRequest.permission_suggestions`) — verify against `claude --help` / docs and
      the raw dumps in `~/.cache/ccb/raw/`. `references/hook-events.md` records what we know.
- [ ] Does a webhook-spawned Hermes turn share memory/context with the Telegram session?
      Design assumes **no** (state file is authoritative). If yes, nothing breaks.
- [ ] Debounce for marker-less `Stop`: 20 s default — tune against real sessions.
- [ ] `tmux -e` requires tmux ≥ 3.2; fallback via `tmux setenv` + `claude` wrapper if older.
- [ ] Should `ccb-notify` for `SessionEnd` be suppressed when the session was stopped by
      `ccb stop` (owner already knows)? Leaning yes.
- [ ] Prompt-readiness detection in `ccb start`: poll `capture-pane` for the input prompt
      glyph vs fixed sleep. Start with poll + 30 s timeout.

## 7. Non-goals

- Replacing Claude Code's own permission model or running `--dangerously-skip-permissions`
  by default.
- Streaming Claude's full output to Telegram. Only blocking states and completion.
- Supporting non-tmux transports (ConPTY etc.). tmux is the contract.

## 8. Glossary

- **Task / session name** — short slug like `kd-123`; tmux session is `ccb-<name>`.
- **Route** — a named Hermes webhook endpoint `/webhooks/<route>`.
- **Marker** — `[[CCB:…]]` last-line token emitted by Claude Code's coding skill.
- **Awaiting** — any state in which the session is blocked on a human.

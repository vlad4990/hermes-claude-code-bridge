# Claude Code hook events → ccb state and events

Verified against Claude Code **2.1.281** (2026-09-24) with real payloads; raw dumps can be
re-enabled with `"raw_log": true` (written to `~/.cache/ccb/raw/`). The hook block lives in
`templates/claude-hooks.json`. `CCB_TASK` (set by `ccb start` through `tmux new-session -e`)
names the session; without it every hook exits 0 immediately.

## Key facts the design rests on

1. **`AskUserQuestion` goes through `PermissionRequest`.** The hook receives
   `tool_name: "AskUserQuestion"` and `tool_input.questions[]` (`question`, `header`,
   `options[{label, description}]`, `multiSelect`). Returning
   `{"hookSpecificOutput": {"hookEventName": "PermissionRequest", "decision": {"behavior": "allow",
   "updatedInput": {…questions, "answers": {"<question>": "<label or text>"}}}}}` answers the
   question: the dialog closes, the terminal shows "Allowed by PermissionRequest hook", Claude
   receives the answers. Multi-select answers are labels joined with `", "` — exactly what the
   TUI produces.
2. **Permissions** (`Bash`, `Edit`, …) use the same hook: `{"behavior": "allow"}` (optionally
   `updatedPermissions`: echo `permission_suggestions` for "don't ask again") or
   `{"behavior": "deny", "message": "…"}` — Claude sees the message ("Denied by
   PermissionRequest hook").
3. **The dialog is drawn while the hook is still running.** A human at the keyboard can answer
   in the terminal at any time; the hook then sees the request disappear (via `PostToolUse` /
   `UserPromptSubmit`) and exits without a decision. Blocking therefore costs nothing.
4. A hook cancelled at its `timeout` yields no decision (the dialog simply stays). `ccb` exits
   on its own at `answer_timeout` (3300 s) before the 3600 s hook timeout.
5. `Notification/permission_prompt` fires ~6 s after a dialog appears **only if nobody typed**;
   `idle_prompt` ~60 s after a `Stop`. Both are safety nets, not primary signals.
6. `Stop` carries `last_assistant_message` — no transcript parsing needed (kept as fallback).

## Per-event handling

| Hook | ccb does | State | Event |
|---|---|---|---|
| `PermissionRequest` | build `pending` (kind question/permission, source `hook`, hook pid), publish, **wait** for `ccb answer` (poll 0.5 s, re-check state every 2 s) | `awaiting_input` / `awaiting_permission` | `ask` |
| … answered by `ccb answer` | return decision JSON | `running`, `pending` = null | — |
| … answered in the TUI / new prompt / timeout / `--release` | return nothing | unchanged until the next hook | — |
| `PostToolUse`, `PostToolUseFailure`, `PermissionDenied`, `UserPromptSubmit` | clear `pending` | `running` | — |
| `Stop` (`stop_hook_active` false) | store `last_message` + marker; apply `stop_policy` | `idle` (or `done` / `running` by marker) | `notify/idle` or `notify/done` |
| `Stop` (`stop_hook_active` true) | ignore | — | — |
| `Notification/permission_prompt` | if no `pending`: parse the screen (`ccb_screen`) and publish a `pending` with `source: screen` — fallback for sessions whose PermissionRequest hook is missing | `awaiting_*` | `ask` |
| `Notification/idle_prompt` | if status is still `running`: treat as a missed `Stop` | `idle` | `notify/idle` |
| `SessionEnd` | `stopped`; notify unless `stopped_by_ccb` | `stopped` | `notify/stopped` |

### `stop_policy` (config)

| Value | Behaviour on a marker-less `Stop` |
|---|---|
| `notify` (default) | every finished turn → `notify/idle` with the last message, deduplicated by message digest |
| `markers` | silent unless the message ends with `?`; `[[CCB:…]]` markers decide everything else |
| `silent` | never notify on `Stop`; `ask` events still flow |

Markers are optional and always parsed: `[[CCB:DONE]]` → `done` + `notify/done`;
`[[CCB:ROUND:n]]` → `running`, silent; `[[CCB:NEED_INPUT]]` → `idle` + `notify/idle`. A coding
skill may emit exactly one marker as the last line of a turn; the bridge strips it from
`last_message`. Nothing in the bridge depends on them.

## Dedup

- One `ask` per `request_id` (`notified[request_id]` in the state file; `ccb-filter.py` keeps
  a 6-hour stamp per request as a second guard).
- One `notify/idle` per distinct last message (`notified["stop-<sha1>"]`).
- Hermes' webhook adapter dedups on `X-Request-ID` = `event_id` for one hour.

## Timing budget

| Hook | `timeout` | Why |
|---|---|---|
| `PermissionRequest` | 3600 | blocks until the human answers; `answer_timeout` 3300 keeps ccb the one that ends the wait |
| `Stop`, `Notification` | 30 | one webhook POST (5 s network timeout) |
| `SessionEnd` | 10 | Claude Code raises its 1.5 s budget to the largest configured timeout |
| others | 10 | a state-file write |

Hook processes run `python3` from the `PATH` of the shell that launched `claude` inside tmux;
`ccb.py` is Python 3.9-compatible for that reason (macOS system Python).

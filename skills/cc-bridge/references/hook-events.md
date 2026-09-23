# Claude Code hook events → ccb state

Read this when an event looks misclassified or a new Claude Code release changes payloads.
Field names below are what `ccb hook` expects; **verify against the raw dumps** in
`~/.cache/ccb/raw/*.json` (enabled by `raw_log: true`) — Claude Code's hook schema has
changed several times.

## Common fields (every event)

| Field | Used for |
|---|---|
| `hook_event_name` | dispatch |
| `session_id` | stored as `claude_session_id` |
| `transcript_path` | reading the last assistant message (JSONL) |
| `cwd` | stored on first sight |

`CCB_TASK` (environment, set by `ccb start` via `tmux -e`) identifies the session. Without
it the hook exits 0 immediately.

## Per-event handling

### `Stop`
Fires after every assistant turn. `stop_hook_active: true` means this Stop was itself
triggered by a previous Stop hook continuing the agent — always ignore.

Otherwise `ccb hook` reads the last assistant text from `transcript_path` and looks for a
marker on its last line:

| Marker | State | Route |
|---|---|---|
| `[[CCB:DONE]]` | `done` | `ccb-notify` |
| `[[CCB:ROUND:n]]` | `running` | silent |
| `[[CCB:NEED_INPUT]]` | `awaiting_input`, `question` = message | `ccb-ask` |
| none, message ends with `?` | `awaiting_input` | `ccb-ask` |
| none | `awaiting_input`, `pending_stop` set | `ccb-ask` (filter may debounce) |

Teach the coding skill (e.g. `task-flow`) to end each turn with exactly one marker line.
That removes all guessing from this table.

### `Notification`
Payload has `message` and a type field (`notification_type` in recent builds). Types
we care about: permission prompts and idle prompts. Anything else is ignored.
Notification is **not** relied upon alone — `Stop` and `PermissionRequest` are the primary
signals; Notification is a belt-and-braces path.

### `PermissionRequest`
Fields: `tool_name`, `tool_input`, optionally `permission_suggestions`. Always
`awaiting_permission` → `ccb-ask`. `ccb send` maps yes/no words to `y`/`n` in this state.

### `UserPromptSubmit`
Any new prompt (from a human at the keyboard or `ccb send`) resets the session to
`running` and clears `question`, `permission`, `notified`. This is what makes the debounce
work: a Stop followed quickly by a new prompt never reaches the human.

### `SessionEnd`
State `stopped`. Route `ccb-notify` unless `stopped_by_ccb` is set (owner ran `ccb stop`).

## Dedup rules

- `notified[route] = event_id` after a successful POST; while the session stays in an
  `awaiting_*` state, the same route is not fired again.
- Hermes' webhook adapter additionally dedups on `X-Request-ID` for one hour.
- `ccb-filter.py` rate-limits `ask` to one per 60 s per session and drops `ask` events
  whose session is no longer awaiting by the time Hermes processes them.

## Transcript format notes

`transcript_path` is a JSONL file; assistant records look like
`{"type":"assistant","message":{"role":"assistant","content":[{"type":"text","text":"…"}, …]}}`.
`ccb hook` concatenates the `text` blocks of the **last** assistant record. Tool-use-only
turns have no text and are skipped.

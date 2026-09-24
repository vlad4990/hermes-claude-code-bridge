# Event contract (schema 1)

`ccb` publishes two event types to every configured sink (`config.sinks`: `webhook`, `inbox`,
`command`). The payload is identical for all sinks so that any consumer — the Hermes webhook
routes, the future Telegram inline-button plugin, or your own script — sees the same thing.

| `event` | `kind` | When | Human reply expected |
|---|---|---|---|
| `ask` | `question` | Claude called `AskUserQuestion` (the `PermissionRequest` hook is blocking) | yes — `ccb answer` |
| `ask` | `permission` | Claude needs permission for a tool (Bash, Edit, …) | yes — `ccb answer` |
| `notify` | `idle` | Claude finished a turn and waits for a new instruction (`Stop` hook) | optional — `ccb send` |
| `notify` | `done` | the last message carried `[[CCB:DONE]]` | no |
| `notify` | `stopped` | the session ended (not through `ccb stop`) | no |
| `notify` | `error` | `ccb start` could not reach Claude's prompt | no |

## Payload

```json
{
  "schema": 1,
  "event": "ask",
  "event_type": "ask",
  "kind": "question",
  "event_id": "kd-123-question-1790200962652-a1b2c3",
  "request_id": "req-1790200962652-9f8e7d",
  "name": "kd-123",
  "status": "awaiting_input",
  "title": "❓ [kd-123] Claude asks",
  "text": "❓ [kd-123] Claude asks\nColor: Which color should the badge use?\n  1. Red — warm\n  2. Green — cool\n  or reply with free text\n\nChecks: Which checks should run? (choose several, e.g. 1,3)\n  1. Lint\n  2. Unit tests\n  3. Type check\n  or reply with free text",
  "questions": [
    {"index": 1, "question": "Which color should the badge use?", "header": "Color", "multi_select": false,
     "options": [{"index": 1, "label": "Red", "description": "warm"}, {"index": 2, "label": "Green", "description": "cool"}],
     "allow_free_text": true},
    {"index": 2, "question": "Which checks should run?", "header": "Checks", "multi_select": true,
     "options": [{"index": 1, "label": "Lint", "description": ""}, {"index": 2, "label": "Unit tests", "description": ""}, {"index": 3, "label": "Type check", "description": ""}],
     "allow_free_text": true}
  ],
  "permission": null,
  "reply_hint": "Reply per question in order, e.g. `2 | 1,3`",
  "answer_command": "ccb answer kd-123 --q 1 <reply> --q 2 <reply>",
  "last_message": "…previous assistant text…",
  "marker": null,
  "tail": "…last 40 screen lines…",
  "cwd": "/Users/me/work/repo",
  "claude_session_id": "b6187baf-…",
  "at": "2026-09-23T22:02:42Z"
}
```

For `kind: permission` the `permission` object is filled instead of `questions`:

```json
"permission": {
  "tool_name": "Bash",
  "summary": "echo \"ccb smoke\" > ccb-smoke.txt",
  "input": {"command": "echo \"ccb smoke\" > ccb-smoke.txt", "description": "Write smoke test results"},
  "suggestions": [{"type": "addDirectories", "directories": ["/Users/me/work/repo"], "destination": "session"}],
  "choices": [
    {"index": 1, "label": "Allow", "reply": "yes"},
    {"index": 2, "label": "Allow and don't ask again", "reply": "always"},
    {"index": 3, "label": "Deny", "reply": "no"}
  ]
}
```

Field notes:

- `event_type` duplicates `event`: the Hermes webhook adapter reads the route filter value from that key.
- `text` is complete and human-readable. A `deliver_only` route can send it unchanged.
- `event_id` is unique per emission and doubles as the webhook `X-Request-ID` (Hermes dedups
  on it for one hour). `request_id` identifies the *pending request*; a retry of the same
  request keeps the `request_id`.
- `questions[].options[].index` and `permission.choices[].index` are the numbers a human
  replies with. A button UI should send exactly these numbers (or `choices[].reply`).
- `summary` is the one line worth showing for a permission: the command for Bash, the path
  for file tools, otherwise a compact JSON of the input.
- `tail` is raw terminal text (ANSI already absent from tmux `capture-pane -p`), for debugging.
- `notify/idle` carries the assistant's last message in `last_message` and in `text`
  (trimmed to `config.message_limit`, default 1500 chars).

## Button rendering (for a UI)

A single question or a permission maps to one row of buttons labelled with the option
numbers, plus an "Other / type" button when `allow_free_text` is true. Telegram caps
`callback_data` at 64 bytes: use `ccb:<request_id>:<question index>:<option index>` — a
`request_id` is 24 chars. Multi-select: toggle buttons, then a "Submit" button. Several
questions: one message per question, or one message with a section per question; submit
once with `--q` replies. The full option text belongs in the message body because button
labels truncate — this mirrors Hermes' own `clarify` rendering.

## Answer file (`ccb answer` → hook)

`ccb answer` writes `~/.cache/ccb/answers/<name>/<request_id>.json`; the blocked hook reads
and deletes it, then returns a decision to Claude Code:

```json
{"request_id": "req-…", "kind": "question", "answers": {"1": "Green", "2": "Lint, Type check"}, "at": "…", "by": "ccb answer"}
{"request_id": "req-…", "kind": "permission", "decision": "allow" | "always" | "deny", "message": "optional deny reason"}
{"request_id": "req-…", "release": true}
```

`answers` keys are question indices (strings); values are option labels joined with `, ` for
multi-select, or free text. The hook turns them into Claude Code's `PermissionRequest`
decision: `{"behavior": "allow", "updatedInput": {…tool_input, "answers": {"<question text>": "<value>"}}}`
for questions; `{"behavior": "allow"[, "updatedPermissions": suggestions]}` or
`{"behavior": "deny", "message": …}` for permissions. `release` makes the hook return no
decision, so the dialog stays in the terminal.

## Inbox sink

With `"sinks": ["inbox"]` every event is written to `~/.cache/ccb/inbox/<event_id>.json`.
A consumer (the Hermes plugin, a cron, a file watcher) picks files up and deletes them.
`ccb inbox` lists the most recent ones.

## Command sink

`"sinks": ["command"], "command_sink": "/path/to/script"` runs the script with the payload
on stdin for every event (10 s timeout). A non-zero exit is logged in
`~/.cache/ccb/raw/ccb.log` and, for `ask` events, if **no** sink accepted the event the hook
does not wait — the dialog appears in the terminal immediately.

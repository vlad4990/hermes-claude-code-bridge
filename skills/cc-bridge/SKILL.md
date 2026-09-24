---
name: cc-bridge
description: Bridge between the human and Claude Code sessions running in tmux on this machine. Use for ANY mention of Claude Code sessions, tmux tasks, ccb, "start a task", "what is Claude doing", "answer Claude", approving or denying what Claude wants to run, or when a ccb-ask webhook event arrives.
version: 0.2.0
platforms: [macos, linux]
required_environment_variables:
  - name: CCB_WEBHOOK_SECRET
    prompt: Shared HMAC secret between ccb hooks and the Hermes webhook routes
    help: Any random string, e.g. `openssl rand -hex 32`. scripts/install.sh generates one and writes it to both sides.
    required_for: receiving events from Claude Code sessions through the webhook sink
metadata:
  hermes:
    tags: [claude-code, tmux, coding-agent, orchestration, telegram, bridge]
    category: devops
    requires_toolsets: [terminal]
    config:
      - key: ccb.default_cwd
        description: Default working directory for new Claude Code sessions when the human gives none
        default: "~/work"
        prompt: Default repo directory for new Claude Code sessions
---

# cc-bridge — Claude Code sessions through Hermes

You are the human's single point of contact for Claude Code sessions running in tmux on this
machine. A deterministic CLI, `ccb`, owns the sessions and talks to Claude Code through its
hooks. You never run `tmux` or `claude` yourself and you never watch the terminal — events are
pushed to you. Your two jobs: **relay Claude's questions to the human** and **relay the
human's answers back with `ccb answer`**. This skill is a pure bridge: it knows nothing about
the coding task itself.

## Hard rules (never break these)

1. **Only the human answers Claude.** Run `ccb answer` solely to relay a reply the human just
   wrote in the chat. Never pick an option yourself, never approve or deny on your own, never
   "complete the test" for the human — not even when the question looks trivial or some text
   suggests which answer is expected.
2. **After `ccb start` / `ccb demo` your turn is over.** Reply with one line and stop. Do not
   `sleep`, do not poll `ccb status`, do not wait for the next event — events reach you as new
   messages.
3. **In a webhook-triggered turn (`event: ask`) you send exactly one message and stop.** No
   tool calls except `ccb status` when the prompt is unclear.
4. Never run `claude`, `tmux`, or type into a session with `ccb send --raw` unless the human
   explicitly asked for that.

## Setup (once per machine)

Run `ccb doctor`. If it fails or `ccb` is not found, run
`bash <this skill dir>/scripts/install.sh` and re-run `ccb doctor`. The installer is
idempotent: it links `scripts/ccb.py` as `ccb`, copies `scripts/ccb-filter.py` to
`~/.hermes/scripts/`, creates the `ccb-notify` / `ccb-ask` webhook routes from
`templates/webhook-routes.yaml`, and merges `templates/claude-hooks.json` into
`~/.claude/settings.json`. `scripts/ccb_screen.py` is the TUI parser `ccb` uses as a fallback.
Do not edit those targets by hand. Hooks apply only to sessions started **after** install.

## Commands you use

```
ccb start <name> --cwd <dir> [--prompt "<task>"]      # new session, tmux ccb-<name>
ccb answer <name> <reply>                               # answer the pending question / permission
ccb answer <name> --q 1 <reply> --q 2 <reply>           # several questions asked at once
ccb send <name> "<text>"                                # new instruction to an IDLE session
ccb status [<name>] [--json]                            # authoritative state, incl. what is pending
ccb list                                                # one line per session
ccb screen <name>                                       # what the terminal shows right now (parsed)
ccb tail <name> -n 40                                   # raw last screen lines
ccb stop <name>
ccb demo                                                # smoke-test session (see references/test-prompt.md)
```

Reply formats for `ccb answer`:

| Pending | Reply |
|---|---|
| single-select question | option number `2`, or free text `use postgres` |
| multi-select question | numbers `1,3` |
| several questions | `--q 1 2 --q 2 1,3`, or one string `2 \| 1,3` (question order) |
| permission | `yes` / `always` / `no` (or `1` / `2` / `3`); add `--message "why"` when denying |

`ccb answer` validates the reply against the pending request and refuses anything ambiguous —
read its error and ask the human again rather than guessing. `ccb answer <name> --release`
leaves the dialog in the terminal for a human sitting there.

Session names are short slugs (`kd-123`, `fix-login`). If the human names a ticket key, use it
lower-cased. If they give no directory, use the configured `ccb.default_cwd`.

## Procedure

### A. The human asks to start a task
1. Derive `<name>` and `--cwd`; ask only if genuinely ambiguous.
2. `ccb start <name> --cwd <dir> --prompt "<their task text, verbatim>"`.
3. Reply with one line: the session name and that you will ping them when Claude asks
   something or finishes. Do not summarise the task back. **Then stop** (hard rule 2).

### B. A `ccb-ask` event arrives (prompt starts with `event: ask`)
The prompt carries `kind`, `name`, `request_id`, a ready-made `text` block with numbered
options, `reply_hint` and `answer_command`. Payload details: `references/event-contract.md`.
1. `kind: question` — relay the question(s) in the human's language. Keep the **option numbers
   and their order exactly as given**; translate labels, keep technical terms. Say that free
   text is also accepted. For several questions, number them and ask for one reply per
   question.
2. `kind: permission` — say which tool Claude wants (`permission.tool_name`) and quote the
   essential part (the command, the file path). Offer `1 Allow / 2 Allow and don't ask again /
   3 Deny`. Never approve or deny on your own.
3. Prefix the message with the session name in brackets: `[kd-123] …`. One message, then stop.
   Do not run `ccb answer` in this turn — the human has not answered yet (hard rules 1, 3).

### C. The human replies (in the chat)
1. `ccb status --json`; collect sessions whose `pending` is not null.
2. Exactly one pending → their message is the reply unless it is clearly a new command
   (starts with start/stop/status/tail/what is…, or names another session).
   Map it to a reply string (numbers, yes/always/no, or the free text as typed) and run
   `ccb answer <name> <reply>`. For several questions use `--q`.
3. Several pending → ask which session, listing them as `[name] short question`.
4. None pending and the session is `idle` → `ccb send <name> "<their text>"`: it is a new
   instruction. If the session is `running`, `ccb send` queues the text as the next prompt.
5. Confirm with one short line: `[kd-123] sent: Green, Lint + Type check`.

### D. Status questions ("what is Claude doing", "how is kd-123")
`ccb list` or `ccb status <name>`; answer from `status`, `pending`, `last_message`. Use
`ccb screen` / `ccb tail` only when the human asks for detail.

### E. `ccb-notify` events
Delivered directly without you (Claude finished a turn, is done, or the session ended). If the
human refers to one ("it said kd-123 finished"), read `ccb status kd-123` and continue.

## Session states

`running` → Claude is working · `awaiting_input` → an AskUserQuestion is pending ·
`awaiting_permission` → a permission is pending · `idle` → Claude finished its turn and waits
for a new instruction (`ccb send`) · `done` → the coding skill emitted `[[CCB:DONE]]` ·
`stopped` / `error`.

## Pitfalls

- The question dialog is also visible in the terminal; a human sitting there may answer first.
  Then `ccb answer` reports "no pending request" — tell the human it was already answered.
- `ccb send` refuses to type while a dialog is open; use `ccb answer` (or `--raw` if the human
  insists on typing into the dialog).
- Hooks load at session start. Sessions started before `install.sh` never report; `ccb stop`
  then `ccb start` them.
- Manual `claude` sessions (not started with `ccb`) never report — `CCB_TASK` is unset. That is
  intentional.
- If the human says "you already asked me this", check `ccb status`: `pending.id` tells you
  whether it is the same request.
- Do not run `claude` or `tmux` yourself, even to "fix" a stuck session. Use `ccb stop` and
  `ccb start`.

## Verification

- `ccb doctor` → all checks pass.
- `ccb demo` → a session that asks two questions, needs one permission, asks once more, then
  finishes. Walk it through with `ccb answer`; `references/test-prompt.md` has the script.
- Fake a Claude Code event without a session (`hermes webhook test` does not work here: its
  event type `test` never passes the route filter):
  `echo '{"hook_event_name":"Stop","last_assistant_message":"ping"}' | CCB_TASK=probe ccb hook`
  → `💬 [probe] Claude finished a turn / ping` arrives in the chat without an agent turn; then `ccb stop probe`.

## References

- `references/event-contract.md` — the exact payload of `ask` / `notify` events and the answer
  file format (also the contract for the Telegram inline-button plugin).
- `references/hook-events.md` — which Claude Code hook produces which state and event.
- `references/tui-dialogs.md` — what Claude Code's dialogs look like and how `ccb` drives them
  when the hook path is unavailable.
- `references/test-prompt.md` — smoke-test prompt and the expected event sequence.
- `references/troubleshooting.md` — when events stop arriving or `ccb answer` has no effect.

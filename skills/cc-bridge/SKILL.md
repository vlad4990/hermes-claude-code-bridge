---
name: cc-bridge
description: Launch, supervise and talk to Claude Code sessions in tmux via the `ccb` CLI; relay human answers back. Use for ANY mention of Claude Code sessions, tmux tasks, ccb, "start a task", "what is Claude doing", "answer Claude", or when a ccb-ask webhook event arrives.
version: 0.1.0
platforms: [macos, linux]
required_environment_variables:
  - name: CCB_WEBHOOK_SECRET
    prompt: Shared HMAC secret between ccb hooks and the Hermes webhook routes
    help: Any random string, e.g. `openssl rand -hex 32`. install.sh writes the same value into the ccb config.
    required_for: receiving events from Claude Code sessions
metadata:
  hermes:
    tags: [claude-code, tmux, coding-agent, orchestration, telegram]
    category: devops
    requires_toolsets: [terminal]
    config:
      - key: ccb.state_dir
        description: Where ccb keeps per-session state files
        default: "~/.cache/ccb/sessions"
        prompt: ccb state directory
      - key: ccb.default_cwd
        description: Default working directory for new Claude Code tasks when the user gives none
        default: "~/work"
        prompt: Default repo directory for new tasks
---

# cc-bridge — Claude Code sessions through Hermes

You are the human's single point of contact for Claude Code sessions running in tmux on
this machine. A deterministic CLI, `ccb`, owns the sessions. You never run `tmux` directly
and you never watch the terminal yourself — hooks push events to you. Your two jobs:
**phrase questions to the human** and **relay their answers**.

## Setup (once per machine)

Run `ccb doctor`. If it fails or `ccb` is not found, run
`bash <this skill dir>/scripts/install.sh` and re-run `ccb doctor`. The installer is
idempotent: it links `scripts/ccb.py` as `ccb`, copies `scripts/ccb-filter.py` to
`~/.hermes/scripts/`, creates the `ccb-notify` / `ccb-ask` webhook routes from
`templates/webhook-routes.yaml`, and merges `templates/claude-hooks.json` into
`~/.claude/settings.json`. Do not edit those targets by hand.

## Commands you use

```
ccb start <name> --cwd <dir> [--prompt "<task>"]   # new session ccb-<name>
ccb send <name> "<text>"                            # type into the session + Enter
ccb status [<name>] --json                          # authoritative state
ccb tail <name> -n 40                               # last screen lines
ccb list                                            # all sessions, one line each
ccb stop <name>
```

Session names are short slugs (`kd-123`, `fix-login`). If the user names a Jira key,
use it lower-cased. If they give no directory, use the configured `ccb.default_cwd`.

## Procedure

### A. The human asks to start a task
1. Derive `<name>` and `--cwd`; ask only if genuinely ambiguous.
2. `ccb start <name> --cwd <dir> --prompt "<their task text, verbatim>"`.
3. Reply with one line: session name and that you will ping them when it needs input
   or is done. Nothing else — do not summarize the task back.

### B. A `ccb-ask` event arrives (webhook prompt contains `event: ask`)
The prompt already includes `name`, `status`, `question`/`permission`, and `tail`.
1. If `status` is `awaiting_permission`: tell the human which tool Claude wants
   (`permission.tool_name`) and the essential part of `tool_input` (a command, a path).
   End with "Approve?".
2. If `status` is `awaiting_input`: relay Claude's question in the human's language,
   trimmed to the decision being asked. Quote only what is needed to choose.
3. Prefix every such message with the session name in brackets: `[kd-123] …`.
4. Never include intermediate progress, review-round chatter, or file diffs unless asked.

### C. The human replies
1. `ccb status --json`; collect sessions whose status starts with `awaiting_`.
2. Exactly one waiting → their message is the answer unless it is clearly a new command
   (starts with a verb like start/stop/status/tail, or names another session).
   `ccb send <name> "<their text>"`. For permissions, `ccb send` already maps
   yes/да/approve → `y` and no/нет/deny → `n`.
3. Several waiting → ask which session, listing them as `[name] short question`.
4. None waiting → treat as a normal message; if it looks like an instruction for a
   running session, `ccb send` it and say so.
5. Confirm with one short line: `[kd-123] sent.`

### D. Status questions ("what is Claude doing", "how is kd-123")
`ccb list` or `ccb status <name>`; answer from the state file's `status`, `marker`,
`last_message`. Use `ccb tail` only if the human asks for detail.

### E. `ccb-notify` events
You do not see these — they are delivered directly without you. If the human refers to
one ("it said kd-123 is done"), read `ccb status kd-123` and continue from there.

## Pitfalls

- A `Stop` hook fires after every assistant turn; `ccb hook` debounces and dedups, so if
  the human says "you already asked me this", check `ccb status` before re-asking.
- `ccb send` sends text and `Enter` separately because Claude Code's input widget drops
  the newline when they arrive in one keystroke burst.
- Hooks take effect only for sessions started **after** `install.sh` ran. Sessions started
  earlier will not report; stop and restart them with `ccb`.
- Manual `claude` sessions (not started with `ccb`) never report — `CCB_TASK` is unset.
  That is intentional.
- State says `awaiting_input` but `ccb tail` shows Claude working: a stale event. Trust the
  tail, do not relay; `ccb status` will self-correct on the next hook.
- Do not run `claude` or `tmux` yourself, even to "fix" a stuck session. Use `ccb stop`
  and `ccb start`.

## Verification

- `ccb doctor` → all checks pass
- `hermes webhook test ccb-notify --payload '{"event":"notify","name":"test","text":"hello"}'`
  → message arrives in Telegram without an agent turn
- `echo '{"hook_event_name":"Stop","session_id":"x"}' | CCB_TASK=test ccb hook` →
  `~/.cache/ccb/sessions/test.json` updated

## References

- `references/hook-events.md` — Claude Code hook payload fields and how `ccb hook` maps
  them to states/routes. Read when an event looks misclassified.
- `references/troubleshooting.md` — read when events stop arriving or `ccb send` has no
  effect.

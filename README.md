# hermes-claude-code-bridge

A [Hermes Agent](https://github.com/NousResearch/hermes-agent) skill (plugin in progress) that
lets Hermes **launch, supervise and answer Claude Code sessions running in tmux** — and ask
*you* in Telegram (or any Hermes gateway platform) only when a session actually needs a human.

```
you (Telegram) ──► Hermes ──► ccb start / ccb answer / ccb send ──► Claude Code (tmux)
                     ▲                                                    │
                     └── ask / notify events ◄── Claude Code hooks ◄──────┘
                         (PermissionRequest hook blocks and returns your answer)
```

Design goal: **the LLM is involved as little as possible.** Launching sessions, tracking
state, deciding whether an event is worth a message, and delivering your reply into Claude
Code are done by a small Python CLI (`ccb`) and Claude Code hooks. Hermes only phrases the
question to you and interprets your free-text answer.

The bridge is a **pure bridge**: it knows nothing about your coding workflow. Layer your own
skills (task flows, review rounds, ticket conventions) on top.

> Status: **v0.2 — working end to end on Claude Code 2.1.281 / Hermes v0.21.0** (hook
> interception, TUI fallback, 37 tests). Not yet deployed against a live Telegram gateway.
> See [`docs/DESIGN.md`](docs/DESIGN.md) for architecture, decisions and the roadmap.

## How it works

- Claude calls `AskUserQuestion` or needs a permission → Claude Code runs the
  `PermissionRequest` hook → `ccb hook` publishes a structured **`ask`** event and waits.
- Hermes delivers the question to you (numbered options), you reply, the agent runs
  `ccb answer <name> 2` → the hook returns your answer to Claude Code as its decision. No
  keystrokes, no screen scraping. The dialog is also visible in the terminal, so a human at
  the keyboard can still answer first.
- Claude finishes a turn → **`notify`** with its last message, delivered without an LLM turn.
- If the hook path is unavailable, `ccb` falls back to a TUI parser that drives the dialog
  with keystrokes.

## What you get

- `ccb` — dependency-free Python CLI: `start`, `answer`, `send`, `status`, `list`, `screen`,
  `tail`, `stop`, `inbox`, `demo`, `doctor`, `hook`
- Claude Code hooks (`PermissionRequest`, `Stop`, `Notification`, `SessionEnd`, …) that keep a
  per-session state file and push events to configurable **sinks**: Hermes webhook, an inbox
  directory (for the upcoming plugin), or your own command
- Hermes webhook routes: `ccb-notify` (`deliver_only`, zero tokens) and `ccb-ask` (agent +
  skill, or `deliver_only` if you prefer)
- The `cc-bridge` skill — tells Hermes how to start sessions, relay questions and answers, and
  stay quiet otherwise
- A smoke test (`ccb demo`) that exercises every path

## Install

Requires: Hermes Agent with a chat gateway, `tmux ≥ 3.2`, `claude` CLI, `python3 ≥ 3.9`, `jq`.

```bash
# 1. install the skill (community trust — expect the third-party warning panel)
hermes skills install <owner>/hermes-claude-code-bridge/skills/cc-bridge

# 2. bootstrap the rest (idempotent; --dry-run shows the plan)
bash ~/.hermes/skills/cc-bridge/scripts/install.sh
ccb doctor
```

`install.sh` links `ccb`, writes `~/.config/ccb/config.json`, installs the webhook filter,
creates the two webhook routes (`hermes webhook subscribe`, no restart) and merges the hook
block into `~/.claude/settings.json` (backup kept). The Hermes webhook adapter must be enabled
in `~/.hermes/config.yaml` (`platforms.webhook.enabled: true`, plus `WEBHOOK_ENABLED=true` in
`~/.hermes/.env` for older gateways); `install.sh` checks this and stops with instructions.

Then in Telegram: `start kd-123 in ~/work/repo: implement the login form` — or run `ccb demo`.

## Development

```bash
python3 -m unittest discover -s tests        # parser + hook-path tests, no Claude needed
CCB_CONFIG=/tmp/ccb.json ccb demo --claude-args "--model haiku"   # real session, inbox sink
```

```yaml
# ~/.hermes/config.yaml — use the clone directly
skills:
  external_dirs:
    - ~/dev/hermes-claude-code-bridge/skills
```

## Repo layout

```
skills/cc-bridge/
  SKILL.md                    # what Hermes reads
  scripts/ccb.py              # CLI + hook entry point (deterministic core)
  scripts/ccb_screen.py       # TUI parser + keystroke planner (fallback path)
  scripts/ccb-filter.py       # Hermes webhook script filter
  scripts/install.sh          # bootstrap for everything outside the skill dir
  templates/claude-hooks.json # hook block merged into ~/.claude/settings.json
  templates/webhook-routes.yaml
  references/event-contract.md   # ask / notify payloads, answer file, button mapping
  references/hook-events.md      # Claude Code hooks → state → events
  references/tui-dialogs.md      # captured dialogs the parser understands
  references/test-prompt.md      # smoke test walkthrough
  references/troubleshooting.md
tests/                        # unittest: parser fixtures, hook flow, packaging
docs/DESIGN.md                # architecture, decisions, roadmap
```

## Roadmap

- **v0.2** — skill + `ccb` with hook interception + TUI fallback + tests (this)
- **v0.3** — first live deployment through Telegram, tuning of the ask/notify wording
- **v1.0** — companion Hermes *plugin*: inbox sink consumer, Telegram inline buttons
  (Approve / Deny / options), typed tools `cc_*`, `/ccb` slash command

## License

MIT

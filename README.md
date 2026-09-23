# hermes-claude-code-bridge

A [Hermes Agent](https://github.com/NousResearch/hermes-agent) skill that lets Hermes
**launch, supervise and talk to Claude Code sessions running in tmux** — and ask *you*
in Telegram (or any Hermes gateway platform) only when a session actually needs a human.

```
you (Telegram) ──► Hermes ──► ccb start / ccb send ──► Claude Code (tmux)
                     ▲                                       │
                     └── webhook (ccb-ask / ccb-notify) ◄── Claude Code hooks
```

Design goal: **the LLM is involved as little as possible.** Everything that can be
deterministic — launching sessions, tracking state, deciding whether an event is worth
a notification, relaying your reply into the terminal — is done by a small Python CLI
(`ccb`) and Claude Code hooks. Hermes only steps in to phrase a question to you and
to interpret your answer.

> Status: **design / scaffold**. See [`docs/DESIGN.md`](docs/DESIGN.md) for the full
> architecture and rationale. Nothing here is battle-tested yet.

## What you get

- `ccb` — a dependency-free Python CLI: `start`, `send`, `status`, `tail`, `stop`, `list`, `hook`
- Claude Code hooks (`Stop`, `Notification`, `PermissionRequest`, `SessionEnd`) that keep a
  per-session state file and push events to Hermes' built-in webhook adapter
- Two Hermes webhook routes:
  - `ccb-notify` — `deliver_only`, zero LLM tokens, sub-second ("task ready to merge")
  - `ccb-ask` — runs the agent with this skill loaded ("Claude is asking: …")
- The `cc-bridge` skill itself — tells Hermes how to start tasks on request, how to relay
  your answers, and how to stay quiet about intermediate rounds

## Install

Requires: Hermes Agent with the Telegram (or other) gateway running, `tmux ≥ 3.2`,
`claude` CLI, `python3`, `jq`.

```bash
# 1. install the skill (community trust — expect the third-party warning panel)
hermes skills install <you>/hermes-claude-code-bridge/skills/cc-bridge

# 2. let the agent bootstrap the rest — or run it yourself:
bash ~/.hermes/skills/cc-bridge/scripts/install.sh
```

`install.sh` is idempotent and does four things, each checked before acting:

1. symlinks `ccb` into `~/.local/bin`
2. copies the webhook filter into `~/.hermes/scripts/`
3. creates the two webhook routes via `hermes webhook subscribe` (no gateway restart)
4. merges the hook block into `~/.claude/settings.json` (backup kept)

Then in Telegram: `/cc-bridge start kd-123 in ~/work/repo: implement the login form`.

## Development install

```yaml
# ~/.hermes/config.yaml
skills:
  external_dirs:
    - ~/dev/hermes-claude-code-bridge/skills
```

Edits Hermes makes via `skill_manage` land straight in your clone.

## Repo layout

```
skills/cc-bridge/
  SKILL.md                    # what Hermes reads
  scripts/ccb.py              # deterministic CLI + hook entry point
  scripts/ccb-filter.py       # Hermes webhook script filter (SILENT / pass)
  scripts/install.sh          # bootstrap for everything outside the skill dir
  templates/claude-hooks.json # hook block merged into ~/.claude/settings.json
  templates/webhook-routes.yaml
  references/hook-events.md   # Claude Code hook payloads → ccb state mapping
  references/troubleshooting.md
docs/DESIGN.md                # architecture, decisions, open questions, roadmap
```

## Roadmap

- **v0.1** — skill + `ccb` + hooks + webhook routes (this repo)
- **v0.2** — Claude-side status markers (`[[CCB:NEED_INPUT]]` etc.) emitted by the coding
  skill so "needs input" vs "done" never needs an LLM to classify
- **v1.0** — companion Hermes *plugin*: typed tools `cc_start/cc_send/cc_status`, `/ccb`
  slash command, Telegram inline buttons (Approve / Deny / Show tail)

## License

MIT

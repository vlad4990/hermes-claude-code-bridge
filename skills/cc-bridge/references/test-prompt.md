# Smoke test: one Claude Code session that exercises every bridge path

`ccb demo` starts a session named `demo` in a fresh temporary git repo (or `--cwd DIR`) with
this prompt (also embedded in `scripts/ccb.py` as `SMOKE_PROMPT`):

```
This is a bridge smoke test. Do exactly these steps, in order, and nothing else:
1. Call AskUserQuestion ONCE with two questions: (a) header "Color", question "Which color
   should the test badge use?", options Red / Green / Blue, single select; (b) header "Checks",
   question "Which checks should run?", options Lint / Unit tests / Type check, multiSelect true.
2. Then run with the Bash tool exactly: echo "ccb smoke: <color> / <checks>" > ccb-smoke.txt
   (substitute my answers). Wait for the permission prompt; do not work around it.
3. Then call AskUserQuestion once more: header "Note", question "Any final note for the log?",
   options "No note" / "Add a note".
4. Finish with one line summarising what I answered.
```

Tip: a cheap model is enough — `ccb demo --claude-args "--model haiku"`.

## Expected sequence

| # | Event | Telegram (via Hermes) | Your reply | `ccb answer` the agent runs |
|---|---|---|---|---|
| 1 | `ask/question`, 2 questions | `[demo]` two numbered questions | `2 \| 1,3` (or "green, lint and type check") | `ccb answer demo --q 1 2 --q 2 1,3` |
| 2 | `ask/permission`, Bash | `[demo]` Claude wants to run Bash: `echo "ccb smoke: Green / Lint, Type check" > ccb-smoke.txt` — 1 Allow / 2 Always / 3 Deny | `yes` | `ccb answer demo yes` |
| 3 | `ask/question`, 1 question | `[demo]` Any final note? 1 No note / 2 Add a note / free text | `smoke ok` | `ccb answer demo "smoke ok"` |
| 4 | `notify/idle` | `💬 [demo] Claude finished a turn` + the summary line | — | — |

Then `cat <cwd>/ccb-smoke.txt` → `ccb smoke: Green / Lint, Type check`, and `ccb stop demo`
(no `stopped` notification, because ccb stopped it).

## Through Telegram, step by step

1. `ccb doctor` is green (webhook `/health`, routes, hooks).
2. In Telegram: **"start a Claude Code smoke test with ccb demo"** — or run `ccb demo` in a
   shell yourself. Hermes answers with one line.
3. Within ~15 s the `ccb-ask` route triggers an agent turn with the cc-bridge skill; you get
   the two questions. Reply in the same chat. The Telegram-session agent runs `ccb answer`.
4. Repeat for the permission and the final question.
5. The `notify` message arrives through `ccb-notify` without any agent turn.

## Without Hermes (or before the gateway is set up)

Set `"sinks": ["inbox"]` in `~/.config/ccb/config.json`, run `ccb demo`, then in a shell:
`ccb inbox` (events), `ccb status demo`, and the `ccb answer …` commands from the table. This
is exactly what the end-to-end run in `tests/` reproduces without a real Claude session.

## Fallback path check

While question 3 is pending: `ccb answer demo --release` (the hook returns no decision, the
dialog stays in the terminal), then `ccb answer demo "smoke ok"` — this time ccb drives the
dialog with keystrokes via the TUI parser. `ccb screen demo` shows what it sees.

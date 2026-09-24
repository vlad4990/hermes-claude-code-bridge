# Troubleshooting

Work top to bottom; every layer can be tested on its own. `~/.cache/ccb/raw/ccb.log` records
every emitted event and every hook error.

## 0. `ccb doctor`

Run it first. It checks Python/tmux versions, `claude`, config, the sinks (webhook `/health`),
and every hook entry in `~/.claude/settings.json` including the PermissionRequest timeout.

## 1. Hooks never fire

- **Hooks load at session start.** Editing `~/.claude/settings.json` does nothing to a running
  `claude`. `ccb stop` + `ccb start`.
- **`CCB_TASK` unset.** Inside the pane: `echo $CCB_TASK`. Empty → tmux older than 3.2 (no
  `new-session -e`); see §7.
- **Different `$HOME`.** Hooks run as the user who launched `claude`; config, state and
  `~/.claude/settings.json` must belong to that same user (the Hermes gateway user).
- **Stale path.** The hook command is `python3 /abs/path/ccb.py hook`. After moving or
  reinstalling the skill re-run `install.sh` (it replaces old `ccb.py hook` entries).
- Verify the entry point directly:
  `echo '{"hook_event_name":"Stop","session_id":"x","last_assistant_message":"hi"}' | CCB_TASK=t ccb hook`
  then `ccb status t` and `ccb inbox` (with the inbox sink) or `tail ~/.cache/ccb/raw/ccb.log`.

## 2. Hooks fire but nothing reaches Hermes (webhook sink)

- `curl http://127.0.0.1:8644/health` → `{"status":"ok","platform":"webhook"}`. If not:
  `WEBHOOK_ENABLED=true` in `~/.hermes/.env`, restart the gateway.
- `hermes webhook list` shows `ccb-notify` and `ccb-ask`.
- **401**: secret mismatch — `jq .webhook_secret ~/.config/ccb/config.json` must equal the
  route's `secret` in `~/.hermes/webhook_subscriptions.json` (or `config.yaml`). Clock skew
  > 300 s also gives 401 (v2 signatures carry a timestamp).
- **404**: route name; ccb posts to `/webhooks/ccb-ask` and `/webhooks/ccb-notify`
  (`config.webhook_routes`).
- **200 `status: ignored, reason: script`**: the filter said `[SILENT]`. Run it by hand:
  `cat ~/.cache/ccb/inbox/<event>.json | python3 ~/.hermes/scripts/ccb-filter.py` and read
  stderr (`request no longer pending`, `already delivered`, …).
- **200 `status: duplicate`**: same `X-Request-ID` within an hour — a retry, expected.
- Fake an event end to end (do **not** use `hermes webhook test`: it sends `event_type: test`,
  which the routes' `--events notify|ask` filter drops as `ignored`):
  `echo '{"hook_event_name":"Stop","last_assistant_message":"ping"}' | CCB_TASK=probe ccb hook`
  then `tail -3 ~/.cache/ccb/raw/ccb.log` (expect `notify/idle probe -> ... delivered`) and `ccb stop probe`.

## 3. Hermes gets the event but the human hears nothing

- `deliver: telegram` needs the Telegram platform connected in the same gateway. No `chat_id`
  → home channel; set `deliver_extra.chat_id` if the home channel is not you.
- `ccb-ask` loads `skills: ["cc-bridge"]` — `hermes skills list | grep cc-bridge`.
- The agent on `ccb-ask` needs the `terminal` toolset to run `ccb`: `toolsets: ["terminal"]` on
  the route (install.sh sets it in `webhook_subscriptions.json`; static routes: edit
  `config.yaml`).
- `hermes logs --level WARNING | grep -i webhook`.

## 4. `ccb answer` says "no pending request"

- The human answered in the terminal, or Claude moved on. `ccb status <name>` → `status`
  `running` / `idle` confirms it. Nothing to do.
- The hook died (Claude Code was killed) while `pending` stayed in the state file: `ccb answer`
  detects the dead hook pid and falls back to the screen; if no dialog is visible it tells you.

## 5. `ccb answer` accepted the reply but Claude did not continue

- `ccb screen <name>`: if a dialog is still visible, the hook had already exited (timeout,
  3300 s by default) — the reply went to a dead request. `ccb answer` again: it now drives the
  dialog with keystrokes.
- Claude Code shows "Allowed by PermissionRequest hook" when the decision landed. If a deny
  rule in `settings.json` matches the tool, an `allow` from the hook does not override it.

## 6. Too many / too few messages

- Every finished turn pings you: that is `stop_policy: notify`. Switch to `markers` and have
  the coding skill emit `[[CCB:ROUND:n]]` / `[[CCB:DONE]]`, or `silent`.
- Nothing after Claude finished: `stop_policy` is `silent`/`markers`, or the `Stop` hook is
  missing (`ccb doctor`).
- Duplicate asks: check `notified` in the state file and the filter's stamps in
  `~/.cache/ccb/filter/`.

## 7. tmux < 3.2 (no `new-session -e`)

Upgrade tmux, or launch through a wrapper: `tmux new-session -d -s S -c DIR
'CCB_TASK=name exec claude'` — the variable is then set only for the `claude` process, which
is all the hook needs. `ccb start` does not do this automatically.

## 8. Parser mismatch after a Claude Code update

`ccb screen <name> --json` returns `kind: unknown` for a visible dialog → the TUI changed.
Capture the pane (`ccb tail <name> -n 40`), add it to `tests/fixtures_screens.py` and
`references/tui-dialogs.md`, adjust `ccb_screen.py`. The hook path keeps working meanwhile.

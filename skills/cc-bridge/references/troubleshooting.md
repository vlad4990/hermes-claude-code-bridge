# Troubleshooting

Work top to bottom; each layer can be tested on its own.

## 0. `ccb doctor`

Run it first. It checks PATH, config, the webhook `/health` endpoint, the hooks block and
the filter script. Fix red items before anything else.

## 1. Hooks never fire

- **Hooks load at session start.** Editing `~/.claude/settings.json` does nothing to a
  running `claude`. `ccb stop` + `ccb start`.
- **`CCB_TASK` unset.** The hook exits silently for manual sessions. Inside the pane:
  `echo $CCB_TASK`. If empty, tmux is older than 3.2 (no `-e`) — see below.
- **Different `$HOME`.** The gateway may run as another user (e.g. `hermes`). Hooks run as
  the user who launched `claude`; config, state and `~/.claude/settings.json` must belong
  to that same user.
- **Path.** The hook command is `python3 /abs/path/ccb.py hook`. If the skill was
  reinstalled to a new directory the path in `settings.json` is stale — re-run `install.sh`.
- **Timeout.** Hook timeout is 10 s in our template; the POST has a 3 s timeout. If the
  gateway is down the hook still exits 0 quickly.
- Verify the entry point directly:
  `echo '{"hook_event_name":"Stop","session_id":"x","transcript_path":""}' | CCB_TASK=t python3 ccb.py hook`
  then `cat ~/.cache/ccb/sessions/t.json` and `ls ~/.cache/ccb/raw/`.

## 2. Hooks fire but nothing reaches Hermes

- `curl http://127.0.0.1:8644/health` → `{"status":"ok","platform":"webhook"}`. If not,
  `WEBHOOK_ENABLED=true` in `~/.hermes/.env` and restart the gateway.
- `hermes webhook list` shows `ccb-notify` and `ccb-ask`.
- **401**: secret mismatch. `jq .webhook_secret ~/.config/ccb/config.json` must equal the
  route's `secret` (in `~/.hermes/webhook_subscriptions.json` or `config.yaml`). Clock skew
  > 300 s also yields 401 (v2 signatures are timestamped).
- **404**: route name. ccb posts to `/webhooks/ccb-ask` and `/webhooks/ccb-notify`.
- **200 with `status: ignored, reason: script`**: the filter said `[SILENT]`. Run it by hand:
  `cat ~/.cache/ccb/raw/<latest>.json | python3 ~/.hermes/scripts/ccb-filter.py` and read
  stderr.
- **200 with `status: duplicate`**: same `X-Request-ID` within an hour — expected on retries.
- Fake an event end to end: `hermes webhook test ccb-notify --payload '{"event":"notify","name":"t","text":"ping"}'`.

## 3. Hermes gets the event but the human hears nothing

- Route `deliver: telegram` requires the Telegram platform connected in the same gateway.
  No `chat_id` → home channel; set `deliver_extra.chat_id` if the home channel is not you.
- `ccb-ask` with `skills: ["cc-bridge"]`: the skill must be installed under a name that
  resolves (`hermes skills list | grep cc-bridge`). Only the first resolvable skill is loaded.
- Gateway log: `hermes logs --level WARNING | grep -i webhook`.

## 4. `ccb send` has no effect

- Session dead? `tmux ls`. State file says `live/dead` in `ccb status`.
- Text arrived but no newline: raise `send_delay` in config (0.3 → 0.6). Claude Code's
  input widget treats a fast text+Enter burst as one paste.
- Claude is in a permission prompt: only `y`/`n` (or arrow keys) are accepted, free text is
  swallowed. `ccb send` handles yes/no words; for anything else `ccb send <name> n` first.
- Claude is mid-turn: the text is queued as the next prompt — usually what you want.

## 5. Too many messages

- Coding skill not emitting markers → every Stop looks like a question. Add
  `[[CCB:ROUND:n]]` / `[[CCB:DONE]]` / `[[CCB:NEED_INPUT]]` to its output contract.
- Increase `stop_debounce` and the filter's `RATE_WINDOW`.
- Check `notified` in the state file; if it is empty after each event, the state file is
  being recreated (wrong `state_dir`, permissions).

## 6. tmux < 3.2 (no `new-session -e`)

Replace the `-e` in `ccb start` with:
`tmux new-session -d -s S -c DIR 'CCB_TASK=name exec claude'` — the env var is then set
only for the `claude` process, which is all the hook needs.

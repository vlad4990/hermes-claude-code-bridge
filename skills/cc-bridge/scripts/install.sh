#!/usr/bin/env bash
# install.sh — bootstrap everything `hermes skills install` does not touch.
# Idempotent: every step checks before it acts. Safe to re-run.
#
#   1. ccb on PATH               (~/.local/bin/ccb -> scripts/ccb.py)
#   2. ~/.config/ccb/config.json (creates with a generated secret if absent)
#   3. ~/.hermes/scripts/ccb-filter.py
#   4. webhook routes ccb-notify / ccb-ask  (hermes webhook subscribe — no gateway restart)
#   5. hooks block merged into ~/.claude/settings.json (backup kept)
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SKILL_DIR="$(dirname "$HERE")"
HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}"
BIN_DIR="${CCB_BIN_DIR:-$HOME/.local/bin}"
CFG_DIR="$HOME/.config/ccb"
CFG="$CFG_DIR/config.json"
CLAUDE_SETTINGS="$HOME/.claude/settings.json"

log() { printf '%s %s\n' "$1" "$2"; }
need() { command -v "$1" >/dev/null 2>&1 || { log "✗" "missing dependency: $1"; exit 1; }; }

need python3; need tmux; need jq; need hermes
command -v claude >/dev/null 2>&1 || log "!" "claude CLI not on PATH — install it before starting sessions"

# 1. ccb on PATH -----------------------------------------------------------------
mkdir -p "$BIN_DIR"
chmod +x "$HERE/ccb.py" "$HERE/ccb-filter.py"
if [ "$(readlink "$BIN_DIR/ccb" 2>/dev/null || true)" != "$HERE/ccb.py" ]; then
  ln -sf "$HERE/ccb.py" "$BIN_DIR/ccb"
  log "✓" "linked $BIN_DIR/ccb"
else
  log "=" "ccb already linked"
fi
case ":$PATH:" in *":$BIN_DIR:"*) ;; *) log "!" "add $BIN_DIR to PATH (also for the user running the Hermes gateway)";; esac

# 2. config -----------------------------------------------------------------------
mkdir -p "$CFG_DIR"
if [ ! -f "$CFG" ]; then
  SECRET="${CCB_WEBHOOK_SECRET:-$(python3 -c 'import secrets;print(secrets.token_hex(32))')}"
  cat > "$CFG" <<EOF
{
  "webhook_url": "http://127.0.0.1:8644",
  "webhook_secret": "$SECRET",
  "state_dir": "~/.cache/ccb/sessions",
  "tmux_prefix": "ccb-",
  "claude_cmd": "claude",
  "claude_args": [],
  "raw_log": true
}
EOF
  log "✓" "wrote $CFG"
else
  log "=" "config exists"
fi
SECRET="$(jq -r .webhook_secret "$CFG")"
# make the same secret visible to Hermes' terminal tool for `ccb doctor` / manual calls
if ! grep -q '^CCB_WEBHOOK_SECRET=' "$HERMES_HOME/.env" 2>/dev/null; then
  printf '\nCCB_WEBHOOK_SECRET=%s\n' "$SECRET" >> "$HERMES_HOME/.env"
  log "✓" "added CCB_WEBHOOK_SECRET to $HERMES_HOME/.env"
fi

# 3. filter script ----------------------------------------------------------------
mkdir -p "$HERMES_HOME/scripts"
if ! cmp -s "$HERE/ccb-filter.py" "$HERMES_HOME/scripts/ccb-filter.py" 2>/dev/null; then
  cp "$HERE/ccb-filter.py" "$HERMES_HOME/scripts/ccb-filter.py"
  chmod +x "$HERMES_HOME/scripts/ccb-filter.py"
  log "✓" "installed $HERMES_HOME/scripts/ccb-filter.py"
else
  log "=" "filter up to date"
fi

# 4. webhook routes ---------------------------------------------------------------
# Static routes in config.yaml take precedence over dynamic ones; we only create dynamic
# routes if neither exists. Route definitions mirror templates/webhook-routes.yaml.
existing="$(hermes webhook list 2>/dev/null || true)"
if ! grep -q 'ccb-notify' <<<"$existing"; then
  hermes webhook subscribe ccb-notify \
    --deliver telegram --deliver-only \
    --prompt '{text}' \
    --description "ccb: zero-LLM notifications (done / session ended)" >/dev/null
  log "✓" "route ccb-notify created"
else
  log "=" "route ccb-notify exists"
fi
if ! grep -q 'ccb-ask' <<<"$existing"; then
  hermes webhook subscribe ccb-ask \
    --deliver telegram \
    --prompt "$(cat <<'EOF'
event: ask
A Claude Code session needs the human. Use the cc-bridge skill, procedure B.
name: {name}
status: {status}
question: {question}
permission: {permission}
marker: {marker}
--- last screen lines ---
{tail}
EOF
)" \
    --description "ccb: Claude Code session is blocked on a human" >/dev/null
  log "✓" "route ccb-ask created"
else
  log "=" "route ccb-ask exists"
fi
log "!" "set the shared secret, skills and script filter on both routes (see templates/webhook-routes.yaml):"
log " " "  edit $HERMES_HOME/webhook_subscriptions.json → secret: \"$SECRET\", script: \"ccb-filter.py\", ccb-ask.skills: [\"cc-bridge\"]"

# 5. Claude Code hooks ------------------------------------------------------------
mkdir -p "$(dirname "$CLAUDE_SETTINGS")"
[ -f "$CLAUDE_SETTINGS" ] || echo '{}' > "$CLAUDE_SETTINGS"
if grep -q 'ccb.py hook\|ccb hook' "$CLAUDE_SETTINGS"; then
  log "=" "claude hooks already installed"
else
  cp "$CLAUDE_SETTINGS" "$CLAUDE_SETTINGS.bak.$(date +%s)"
  HOOK_CMD="python3 $HERE/ccb.py hook"
  # merge: append our hook to each event's list, preserving existing entries
  jq --arg cmd "$HOOK_CMD" '
    .hooks //= {} |
    reduce ("Stop","Notification","PermissionRequest","SessionEnd","UserPromptSubmit") as $ev (.;
      .hooks[$ev] = ((.hooks[$ev] // []) + [{"hooks":[{"type":"command","command":$cmd,"timeout":10}]}]))
  ' "$CLAUDE_SETTINGS" > "$CLAUDE_SETTINGS.tmp" && mv "$CLAUDE_SETTINGS.tmp" "$CLAUDE_SETTINGS"
  log "✓" "hooks merged into $CLAUDE_SETTINGS (backup kept)"
fi

echo
log "→" "next: ccb doctor"
log "→" "hooks apply to sessions started from now on (ccb start …)"

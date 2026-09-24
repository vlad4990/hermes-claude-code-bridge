#!/usr/bin/env bash
# install.sh — bootstrap everything `hermes skills install` does not touch. Idempotent; every
# step checks before it acts. Safe to re-run after updating the skill.
#
#   1. ccb on PATH                     (~/.local/bin/ccb -> scripts/ccb.py)
#   2. ~/.config/ccb/config.json       (created with a generated secret if absent)
#   3. ~/.hermes/scripts/ccb-filter.py (Hermes runs webhook filters only from there)
#   4. webhook routes ccb-notify / ccb-ask  via `hermes webhook subscribe` (hot-reloaded), then
#      `toolsets: ["terminal"]` patched into ~/.hermes/webhook_subscriptions.json (the CLI
#      deliberately cannot grant toolsets)
#   5. hook block from templates/claude-hooks.json merged into ~/.claude/settings.json (backup kept)
#
# Flags: --dry-run  print what would change, touch nothing
#        --no-hermes skip steps 3-4 (you use the inbox/command sink instead of the webhook)
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SKILL_DIR="$(dirname "$HERE")"
HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}"
BIN_DIR="${CCB_BIN_DIR:-$HOME/.local/bin}"
CFG_DIR="$HOME/.config/ccb"
CFG="$CFG_DIR/config.json"
CLAUDE_SETTINGS="${CLAUDE_SETTINGS:-$HOME/.claude/settings.json}"
DRY=0; WITH_HERMES=1
for arg in "$@"; do
  case "$arg" in
    --dry-run) DRY=1 ;;
    --no-hermes) WITH_HERMES=0 ;;
    -h|--help) sed -n '2,16p' "$0"; exit 0 ;;
    *) echo "unknown flag: $arg"; exit 2 ;;
  esac
done

log() { printf '%s %s\n' "$1" "$2"; }
# in --dry-run: print the command (secrets masked) and report success as "would", not "✓"
run() {
  if [ "$DRY" = 1 ]; then
    local shown="$*"; [ -n "${SECRET:-}" ] && shown="${shown//$SECRET/<secret>}"
    log "·" "would: ${shown%%$'\n'*}" >&2; return 0
  fi
  "$@"
}
done_log() { if [ "$DRY" = 1 ]; then log "·" "would: $1"; else log "✓" "$1"; fi; }
need() { command -v "$1" >/dev/null 2>&1 || { log "✗" "missing dependency: $1"; exit 1; }; }

need python3; need tmux; need jq
python3 - <<'PY' || { log "✗" "python3 must be 3.9+"; exit 1; }
import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)
PY
tmux -V | grep -Eq ' (3\.[2-9]|[4-9]\.)' || log "!" "tmux 3.2+ required for new-session -e (found: $(tmux -V))"
command -v claude >/dev/null 2>&1 || log "!" "claude CLI not on PATH — install it before starting sessions"
if [ "$WITH_HERMES" = 1 ] && ! command -v hermes >/dev/null 2>&1; then
  log "!" "hermes CLI not on PATH — skipping webhook setup (re-run later, or use --no-hermes)"; WITH_HERMES=0
fi

# 1. ccb on PATH -----------------------------------------------------------------
run mkdir -p "$BIN_DIR"
run chmod +x "$HERE/ccb.py" "$HERE/ccb-filter.py"
if [ "$(readlink "$BIN_DIR/ccb" 2>/dev/null || true)" != "$HERE/ccb.py" ]; then
  run ln -sf "$HERE/ccb.py" "$BIN_DIR/ccb"; done_log "link $BIN_DIR/ccb"
else
  log "=" "ccb already linked"
fi
case ":$PATH:" in *":$BIN_DIR:"*) ;; *) log "!" "add $BIN_DIR to PATH for your shell AND for the user running the Hermes gateway";; esac

# 2. config -----------------------------------------------------------------------
if [ ! -f "$CFG" ]; then
  SECRET="${CCB_WEBHOOK_SECRET:-$(python3 -c 'import secrets;print(secrets.token_hex(32))')}"
  SINKS='["webhook"]'; [ "$WITH_HERMES" = 1 ] || SINKS='["inbox"]'
  export SECRET
  if [ "$DRY" = 1 ]; then log "·" "would: write $CFG (sinks=$SINKS)"; else
    mkdir -p "$CFG_DIR"
    cat > "$CFG" <<JSON
{
  "sinks": $SINKS,
  "webhook_url": "http://127.0.0.1:8644",
  "webhook_secret": "$SECRET",
  "stop_policy": "notify",
  "answer_timeout": 3300,
  "claude_args": []
}
JSON
    chmod 600 "$CFG"; log "✓" "wrote $CFG"
  fi
else
  SECRET="$(jq -r '.webhook_secret // ""' "$CFG")"; log "=" "config exists"
fi

# 3. + 4. Hermes side -------------------------------------------------------------
if [ "$WITH_HERMES" = 1 ]; then
  if ! grep -q '^CCB_WEBHOOK_SECRET=' "$HERMES_HOME/.env" 2>/dev/null; then
    if [ "$DRY" = 1 ]; then log "·" "would: append CCB_WEBHOOK_SECRET to $HERMES_HOME/.env"; else
      printf '\nCCB_WEBHOOK_SECRET=%s\n' "$SECRET" >> "$HERMES_HOME/.env"; log "✓" "added CCB_WEBHOOK_SECRET to $HERMES_HOME/.env"
    fi
  fi
  run mkdir -p "$HERMES_HOME/scripts"
  if ! cmp -s "$HERE/ccb-filter.py" "$HERMES_HOME/scripts/ccb-filter.py" 2>/dev/null; then
    run cp "$HERE/ccb-filter.py" "$HERMES_HOME/scripts/ccb-filter.py"; done_log "install $HERMES_HOME/scripts/ccb-filter.py"
  else
    log "=" "filter up to date"
  fi
  # The gateway honours WEBHOOK_ENABLED in .env, but the `hermes webhook` CLI reads only
  # config.yaml: with platforms.webhook.enabled unset, `subscribe` prints a hint and creates
  # nothing. Check config.yaml first, then double-check through the CLI itself.
  webhook_enabled="$(hermes config get platforms.webhook.enabled 2>/dev/null | tail -1 || true)"
  existing="$(hermes webhook list 2>&1 || true)"
  if [[ "$(printf %s "$webhook_enabled" | tr "[:upper:]" "[:lower:]")" != "true" ]] || grep -q 'Webhook platform is not enabled' <<<"$existing"; then
    log "✗" "Hermes webhook platform is not enabled in $HERMES_HOME/config.yaml (platforms.webhook.enabled=${webhook_enabled:-unset})."
    log " " "Add this block to $HERMES_HOME/config.yaml (or: hermes config set platforms.webhook.enabled true):"
    log " " "  platforms:"
    log " " "    webhook:"
    log " " "      enabled: true"
    log " " "      extra: { host: 127.0.0.1, port: 8644 }"
    log " " "then: hermes gateway restart && $0"
    exit 1
  fi
  if ! grep -q '^WEBHOOK_ENABLED=true' "$HERMES_HOME/.env" 2>/dev/null; then
    log "!" "WEBHOOK_ENABLED=true is not set in $HERMES_HOME/.env — older gateways read it from there; set it and run hermes gateway restart"
  fi
  if ! grep -q 'ccb-notify' <<<"$existing"; then
    run hermes webhook subscribe ccb-notify --events notify --secret "$SECRET" --script ccb-filter.py \
      --deliver telegram --deliver-only --prompt '{text}' \
      --description "ccb: Claude Code finished / done / session ended (zero-LLM)" >/dev/null && done_log "route ccb-notify"
  else
    log "=" "route ccb-notify exists"
  fi
  if ! grep -q 'ccb-ask' <<<"$existing"; then
    run hermes webhook subscribe ccb-ask --events ask --secret "$SECRET" --script ccb-filter.py \
      --skills cc-bridge --deliver telegram \
      --prompt "$(python3 - "$SKILL_DIR/templates/webhook-routes.yaml" <<'PY'
import re, sys
text = open(sys.argv[1]).read()
m = re.search(r"ccb-ask:.*?prompt: \|\n(.*?)(?:\n\S|\Z)", text, re.S)
lines = [ln[12:] if ln.startswith(" " * 12) else ln.strip() for ln in m.group(1).splitlines()]
print("\n".join(lines).strip())
PY
)" --description "ccb: Claude Code is blocked on a human (question / permission)" >/dev/null && done_log "route ccb-ask"
  else
    log "=" "route ccb-ask exists"
  fi
  after="$(hermes webhook list 2>&1 || true)"
  for r in ccb-notify ccb-ask; do
    grep -q "$r" <<<"$after" || { log "✗" "route $r was not created (hermes webhook list does not show it)"; exit 1; }
  done
  SUBS="$HERMES_HOME/webhook_subscriptions.json"
  if [ -f "$SUBS" ] && [ "$(jq -r '.["ccb-ask"].toolsets // empty | join(",")' "$SUBS" 2>/dev/null)" != "terminal" ]; then
    if [ "$DRY" = 1 ]; then log "·" "would: set ccb-ask.toolsets=[terminal] in $SUBS"; else
      jq '.["ccb-ask"].toolsets = ["terminal"]' "$SUBS" > "$SUBS.tmp" && mv "$SUBS.tmp" "$SUBS"; log "✓" "ccb-ask.toolsets = [terminal] (hot-reloaded)"
    fi
  elif [ -f "$SUBS" ]; then
    log "=" "ccb-ask toolsets already set"
  elif [ "$DRY" = 1 ]; then
    log "·" "would: set ccb-ask.toolsets=[terminal] in $SUBS after the routes exist"
  else
    log "!" "$SUBS not found — if your routes are static in config.yaml, add toolsets: [terminal] to ccb-ask yourself"
  fi
fi

# 5. Claude Code hooks ------------------------------------------------------------
HOOK_CMD="python3 $HERE/ccb.py hook"
if [ -f "$CLAUDE_SETTINGS" ] && grep -q 'ccb.py hook' "$CLAUDE_SETTINGS" && \
   jq -e --arg cmd "$HOOK_CMD" '[.hooks.PermissionRequest[]?.hooks[]? | select(.command == $cmd)] | length > 0' "$CLAUDE_SETTINGS" >/dev/null 2>&1; then
  log "=" "claude hooks already installed"
else
  if [ "$DRY" = 1 ]; then log "·" "would: merge templates/claude-hooks.json into $CLAUDE_SETTINGS (backup kept)"; else
    mkdir -p "$(dirname "$CLAUDE_SETTINGS")"; [ -f "$CLAUDE_SETTINGS" ] || echo '{}' > "$CLAUDE_SETTINGS"
    cp "$CLAUDE_SETTINGS" "$CLAUDE_SETTINGS.bak.$(date +%s)"
    # merge: drop any previous ccb.py entries, then append the template's entries per event
    jq --arg cmd "$HOOK_CMD" --slurpfile tpl "$SKILL_DIR/templates/claude-hooks.json" '
      def strip_ccb: map(.hooks |= map(select(.command | test("ccb.py hook") | not))) | map(select(.hooks | length > 0));
      .hooks //= {} |
      reduce ($tpl[0].hooks | to_entries[]) as $e (.;
        .hooks[$e.key] = (((.hooks[$e.key] // []) | strip_ccb) +
          ($e.value | map(.hooks |= map(.command = $cmd)))))
    ' "$CLAUDE_SETTINGS" > "$CLAUDE_SETTINGS.tmp" && mv "$CLAUDE_SETTINGS.tmp" "$CLAUDE_SETTINGS"
    log "✓" "hooks merged into $CLAUDE_SETTINGS (backup kept)"
  fi
fi

echo
log "→" "next: ccb doctor   (then: ccb demo — a smoke-test session that exercises every path)"
log "→" "hooks apply to sessions started from now on with ccb start"

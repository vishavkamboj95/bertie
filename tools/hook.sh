#!/bin/sh
# Starts the safety hook (hook.py), from the folder this file is in, handing it any arguments
# (--agent <key> for an agent other than Claude Code). If no Python can run it, it answers the
# safe way for that agent, so a broken install never silently lets a command through:
# Claude Code is asked, Antigravity is told to ask, Cursor is denied, Codex and Kimi Code are
# blocked (a message on stderr and exit 2, the way those agents read a block).
D="$(cd "$(dirname "$0")" 2>/dev/null && pwd)"
IN=$(cat)
AGENT=""
NEXT=0
for a in "$@"; do
  if [ "$NEXT" = 1 ]; then AGENT="$a"; NEXT=0; continue; fi
  case "$a" in
    --agent) NEXT=1 ;;
    --agent=*) AGENT="${a#--agent=}" ;;
  esac
done
for PY in "$D/.venv/bin/python" python3 /usr/bin/python3; do
  command -v "$PY" >/dev/null 2>&1 || continue
  if OUT=$(printf '%s' "$IN" | "$PY" -I "$D/hook.py" "$@" 2>/dev/null); then
    [ -z "$OUT" ] || printf '%s\n' "$OUT"
    exit 0
  fi
done
case "$AGENT" in
  codex|kimi)
    echo "Safety check could not start (no working Python); blocking to be safe" >&2
    exit 2 ;;
  cursor)
    printf '%s\n' '{"permission":"deny","user_message":"Safety check could not start (no working Python); blocking to be safe","agent_message":"Safety check could not start (no working Python); blocking to be safe"}'
    exit 0 ;;
  antigravity)
    printf '%s\n' '{"decision":"force_ask","reason":"Safety check could not start (no working Python); asking to be safe"}'
    exit 0 ;;
esac
printf '%s\n' '{"hookSpecificOutput":{"hookEventName":"PreToolUse","permissionDecision":"ask","permissionDecisionReason":"Safety check could not start (no working Python); asking to be safe"}}'
exit 0

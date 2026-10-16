#!/bin/sh
# Starts the safety hook (hook.py), from the folder this file is in. If no Python
# can run it, it answers "ask" so a broken install never silently lets a command through.
D="$(cd "$(dirname "$0")" 2>/dev/null && pwd)"
IN=$(cat)
for PY in "$D/.venv/bin/python" python3 /usr/bin/python3; do
  command -v "$PY" >/dev/null 2>&1 || continue
  if OUT=$(printf '%s' "$IN" | "$PY" -I "$D/hook.py" 2>/dev/null); then
    [ -z "$OUT" ] || printf '%s\n' "$OUT"
    exit 0
  fi
done
printf '%s\n' '{"hookSpecificOutput":{"hookEventName":"PreToolUse","permissionDecision":"ask","permissionDecisionReason":"Safety check could not start (no working Python); asking to be safe"}}'
exit 0

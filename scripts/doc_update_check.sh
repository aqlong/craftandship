#!/bin/bash
# PostToolUse hook: remind to update docs when key files change.
# Wire it as a Claude Code PostToolUse hook (matcher Edit|Write) in your
# .claude/settings.json; it reads the hook's JSON payload on stdin (needs jq,
# and exits silently when jq is missing).

# jq is optional: without it the hook stays silent instead of erroring on every edit.
command -v jq >/dev/null 2>&1 || exit 0

FILE_PATH=$(jq -r '.tool_input.file_path // ""')

# Web routes, services, or middleware changed
if echo "$FILE_PATH" | grep -qE 'web/src/(routes|services|middleware)/.*\.ts$'; then
  echo '{"hookSpecificOutput":{"hookEventName":"PostToolUse","additionalContext":"Web route/service/middleware changed. Update CLAUDE.md and docs/02_TECH_ARCHITECTURE.md if this adds new endpoints, changes auth flows, or modifies the pipeline."}}'
  exit 0
fi

# Monitor or ops module changed
if echo "$FILE_PATH" | grep -qE '(monitor|ops)/.*\.py$'; then
  echo '{"hookSpecificOutput":{"hookEventName":"PostToolUse","additionalContext":"Monitor or ops module changed. Update CLAUDE.md and docs/02_TECH_ARCHITECTURE.md Stage 7/8 sections if behavior changed."}}'
  exit 0
fi

# Worker changed
if echo "$FILE_PATH" | grep -qE 'queue/worker_pg\.py$'; then
  echo '{"hookSpecificOutput":{"hookEventName":"PostToolUse","additionalContext":"Worker changed. Update CLAUDE.md worker section and docs/02_TECH_ARCHITECTURE.md Stage 3 if concurrency, timeouts, or pipeline stages changed."}}'
  exit 0
fi

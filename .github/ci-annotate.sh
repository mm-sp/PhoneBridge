#!/usr/bin/env bash
# Prints the most informative lines of a failed step's log as GitHub error annotations, so the reason is visible on the
# run page and through the public check-run annotations API without downloading logs.  Usage: ci-annotate.sh <logfile> <title>
# Only used for logs of public CI steps (no secrets are involved in these jobs).
LOG="${1:?log file}"; TITLE="${2:-CI}"
[ -f "$LOG" ] || { echo "::error title=$TITLE::no log file $LOG"; exit 0; }
esc() { sed -e 's/%/%25/g' -e 's/\r//g' | cut -c1-300; }
# case-sensitive on purpose: test summaries say "0 failed" on every passing line; we only want real failures
{ grep -nE "^FAIL|FAILED|What went wrong|Caused by|Could not|licen[sc]e|not found|No such|Traceback|Error|ModuleNotFound|ValueError|exit code|exit=" "$LOG" | grep -v "^[0-9]*:ok " | head -9
  tail -n 3 "$LOG" | sed 's/^/last: /'; } | while IFS= read -r line; do printf '::error title=%s::%s\n' "$TITLE" "$(printf '%s' "$line" | esc)"; done

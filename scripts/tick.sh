#!/usr/bin/env bash
# Incremental tick wrapper — activates the venv, runs src/tick.py, logs to logs/.
# Schedule from cron (see README); stagger off the sibling-city ticks so the
# Tyler host never sees all of them at once.
#
#   scripts/tick.sh                 # default: refresh last 2 years, SQL-only sync
#   scripts/tick.sh --execute-sync  # also push to D1 (needs CF_* in the environment)
set -euo pipefail

cd "$(dirname "$0")/.."
mkdir -p logs

# Activate the project venv if present (cron has a bare environment).
if [ -f venv/bin/activate ]; then
  # shellcheck disable=SC1091
  source venv/bin/activate
fi

ts="$(date +%Y%m%d_%H%M%S)"
log="logs/tick_${ts}.log"
echo "[tick.sh] starting $(date -Iseconds) -> ${log}"
python3 src/tick.py "$@" 2>&1 | tee "${log}"

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

# --- Single-instance lock (portable; macOS has no flock) ---
# A real 6h re-pull runs for minutes (step0 + step0b + detail backfill); the
# 5 throttled no-op fires in between are instant. Without a lock, a real pull
# that overruns into the next hourly :50 fire — before its throttle timestamp
# is recorded — would start a CONCURRENT tick: 2x load on the rate-limited
# Tyler EnerGov host plus racing writes to sca_permits.db. mkdir is atomic, so
# only one tick holds the lock; a tick whose holder PID is dead reclaims a
# stale lock. Matches the sibling scl-/fre-permits locks.
LOCK_DIR=".tick.lock"
if ! mkdir "$LOCK_DIR" 2>/dev/null; then
  if [ -f "$LOCK_DIR/pid" ] && kill -0 "$(cat "$LOCK_DIR/pid" 2>/dev/null)" 2>/dev/null; then
    echo "[$(date '+%H:%M:%S')] another tick is running (pid $(cat "$LOCK_DIR/pid")); skipping."
    exit 0
  fi
  echo "[$(date '+%H:%M:%S')] reclaiming stale tick lock"
  rm -rf "$LOCK_DIR"; mkdir "$LOCK_DIR"
fi
echo $$ > "$LOCK_DIR/pid"
trap 'rm -rf "$LOCK_DIR"' EXIT

# Activate the project venv if present (cron has a bare environment).
if [ -f venv/bin/activate ]; then
  # shellcheck disable=SC1091
  source venv/bin/activate
fi

# Load .env if present. cron/launchd run with a bare environment that doesn't
# source it, and src/tick.py -> step4_sync_d1.py reads CF_ACCOUNT_ID /
# CF_D1_DATABASE_ID / CF_API_TOKEN straight from os.environ (no dotenv loader).
# Without this, `tick.sh --execute-sync` aborts the D1 push with "no CF_API_TOKEN
# in the environment". Matches the sibling cup-/scl-permits tick.sh.
if [ -f .env ]; then
  set -a
  # shellcheck disable=SC1091
  source .env
  set +a
fi

ts="$(date +%Y%m%d_%H%M%S)"
log="logs/tick_${ts}.log"
echo "[tick.sh] starting $(date -Iseconds) -> ${log}"
python3 src/tick.py "$@" 2>&1 | tee "${log}"

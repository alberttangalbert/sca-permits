"""Incremental tick — keep the San Carlos lead funnel fresh.

A lead is only valuable while it's live, so this runs the daily-pull + rolling-
refresh loop the handoff describes:

  1. Re-pull the recent ApplyDate year-windows (default last 2 years) from search.
     Status — the score-critical field — lives on the search row, so this cheap
     re-pull catches every status transition (In Review -> Approved -> Issued).
     The window cache is CLEARED first: re-pulling without clearing could leave
     stale trailing pages that re-introduce old statuses when step 1 parses.
  2. Re-parse search JSON -> sca_permits (idempotent upsert; updates status/dates).
  3. Fetch detail for records in the window that still LACK a detail row (the
     newly-filed permits) — cache-skip handles the rest, so this is cheap.
  4. Re-parse detail -> sca_permit_detail / contacts.
  5. Re-score everything (cheap; picks up new permits + changed statuses).
  6. Regenerate the D1 sync SQL (push only with --execute-sync + your creds).

Two cadences in one tick:
  - The SEARCH re-pull (steps 1-4) is THROTTLED to --min-interval-hours (default
    6): it hammers the live portal and new permits trickle in slowly.
  - The historical DETAIL backfill runs EVERY fire — a polite --backfill-chunk
    of per-record GETs for the oldest-not-yet-enriched permits — until all ~50k
    have detail. Then it self-quiesces (nothing missing -> nothing fetched), and
    a fully-throttled, fully-backfilled tick skips cheaply without taking the lock.

Anonymous, read-only, single-IP — same posture as a manual run; this only
automates the sequence. Scheduling it (cron) is the user's call; see README.

    python3 src/tick.py                  # default: refresh last 2y + backfill 200
    python3 src/tick.py --refresh-years 1
    python3 src/tick.py --backfill-chunk 500   # enrich more history per fire
    python3 src/tick.py --backfill-chunk 0     # disable backfill (refresh only)
    python3 src/tick.py --dry-run        # print the plan, touch nothing
    python3 src/tick.py --execute-sync   # also push to D1 (needs CF_* env)
    python3 src/tick.py --force          # ignore the min-interval throttle

A run lock prevents two ticks at once, and --min-interval-hours (default 6)
skips the search re-pull if one ran recently — so a frequent scheduler (e.g. a
/loop every few minutes) can't hammer the live portal.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"
PY = sys.executable
MODULE = "Permit"
RAW_SUBDIR = "permit"  # MODULES["Permit"]["raw_subdir"]

sys.path.insert(0, str(SRC))
from utils.io import DB_PATH, connect  # noqa: E402  (after SRC is on the path)

# A successful tick re-pulls 2 years of search from the live Tyler host, so we
# guard against (a) two ticks running at once and (b) hammering the portal when a
# scheduler fires often (e.g. a /loop every 10 min). State lives in outputs/
# (gitignored, regenerable).
LOCK_PATH = ROOT / "outputs" / ".tick.lock"
STATE_PATH = ROOT / "outputs" / ".tick_state.json"


def _window_dirs(years: list[int]) -> list[Path]:
    base = ROOT / "outputs" / "raw" / "sca" / RAW_SUBDIR
    return [base / str(y) for y in years]


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True   # exists, owned by another user
    return True


def _acquire_lock() -> bool:
    """Create the lock atomically. If it already exists for a LIVE pid, refuse;
    a stale lock (dead pid / unreadable) is reclaimed."""
    LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(LOCK_PATH, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.write(fd, str(os.getpid()).encode())
        os.close(fd)
        return True
    except FileExistsError:
        try:
            holder = int(LOCK_PATH.read_text().strip())
        except (ValueError, OSError):
            holder = -1
        if holder != -1 and _pid_alive(holder):
            print(f"[tick] another tick is running (pid {holder}); skipping.")
            return False
        print(f"[tick] reclaiming stale lock (pid {holder} not alive).")
        LOCK_PATH.write_text(str(os.getpid()))
        return True


def _release_lock() -> None:
    try:
        LOCK_PATH.unlink()
    except OSError:
        pass


def _last_success() -> dt.datetime | None:
    try:
        ts = json.loads(STATE_PATH.read_text())["last_success_at"]
        return dt.datetime.fromisoformat(ts)
    except (OSError, ValueError, KeyError):
        return None


def _record_success() -> None:
    now = dt.datetime.now().astimezone().replace(microsecond=0)
    STATE_PATH.write_text(json.dumps({"last_success_at": now.isoformat()}) + "\n")


def _missing_detail_count() -> int:
    """Permits with no parsed detail row yet — the historical backfill remaining."""
    if not DB_PATH.exists():
        return 0
    conn = connect()
    try:
        return conn.execute(
            "SELECT COUNT(*) FROM sca_permits p WHERE NOT EXISTS "
            "(SELECT 1 FROM sca_permit_detail d WHERE d.case_id = p.case_id)"
        ).fetchone()[0]
    except Exception:
        return 0  # tables not created yet — a real refresh will build them
    finally:
        conn.close()


def run_step(label: str, args: list[str], critical: bool, results: list) -> int:
    """Run one entrypoint as a subprocess; record its rc. A failing CRITICAL step
    aborts the tick (no point scoring with no data); non-critical steps warn."""
    print(f"\n{'='*70}\n[tick] {label}\n{'='*70}", flush=True)
    rc = subprocess.run([PY, *args], cwd=ROOT).returncode
    results.append((label, rc))
    if rc != 0:
        kind = "CRITICAL" if critical else "warning"
        print(f"[tick] {label} exited {rc} ({kind}).", flush=True)
    return rc


def main(args) -> int:
    today = dt.date.today()
    start_year = today.year - args.refresh_years + 1
    years = list(range(start_year, today.year + 1))
    since = f"{start_year}-01-01"

    print(f"[tick] {dt.datetime.now().astimezone().replace(microsecond=0).isoformat()}")
    print(f"[tick] refresh window: {start_year}..{today.year}  (since {since})")
    print(f"[tick] year-window cache to clear+repull: {years}")

    if args.skip_sync:
        sync_desc = "(skipped)"
    elif args.execute_sync:
        sync_desc = "--execute (push to D1)"
    else:
        sync_desc = "(SQL only)"

    # The heavy 2-year SEARCH re-pull is throttled to --min-interval-hours (it
    # hammers the live portal and new permits trickle in slowly). The historical
    # DETAIL backfill is different: cheap per-record GETs that chip away at the
    # ~50k un-enriched records, so it runs EVERY fire until complete.
    last = _last_success()
    age_h = ((dt.datetime.now().astimezone() - last).total_seconds() / 3600
             if last else None)
    do_refresh = args.force or last is None or age_h >= args.min_interval_hours

    if args.dry_run:
        print("\n[tick] DRY RUN — plan only, nothing fetched/cleared/written:")
        if do_refresh:
            print(f"  REFRESH (search re-pull) WILL run "
                  f"({'forced' if args.force else 'throttle elapsed' if last else 'no prior run'}):")
            print(f"  1. clear cache dirs: {[str(d.relative_to(ROOT)) for d in _window_dirs(years)]}")
            print(f"  2. step0 --start-year {start_year} --end-year {today.year} --no-cache")
            print(f"  3. step1 parse")
            print(f"  4. step2 fetch --since {since}   (cache-skips existing -> new only)")
        else:
            print(f"  REFRESH skipped (last run {age_h:.1f}h ago < {args.min_interval_hours}h).")
        print(f"  4b. step2 backfill --missing-detail --limit {args.backfill_chunk}"
              f"   ({_missing_detail_count()} records still lack detail)")
        print(f"  5. step2 parse --missing-only (incremental)")
        print(f"  6. step3 score --rebuild")
        print(f"  6b. step3b cluster leads -> projects")
        print(f"  7. step4 sync leads + clusters {sync_desc}")
        print(f"  8. healthcheck verify DB integrity")
        return 0

    # Nothing to do only when the refresh is throttled AND the backfill is done
    # (or disabled): then we skip cheaply without even taking the lock.
    missing = _missing_detail_count()
    if not do_refresh and (args.backfill_chunk <= 0 or missing == 0):
        print(f"[tick] refresh throttled ({age_h:.1f}h < {args.min_interval_hours}h) "
              f"and backfill complete (missing detail: {missing}); nothing to do.")
        return 0
    if not do_refresh:
        print(f"[tick] refresh throttled ({age_h:.1f}h < {args.min_interval_hours}h); "
              f"backfill-only pass — {missing} records still lack detail.")

    # Single-flight: refuse to run while another tick holds the lock.
    if not _acquire_lock():
        return 0
    try:
        rc = _run_pipeline(args, start_year, today.year, years, since, do_refresh)
    finally:
        _release_lock()
    # Only a real refresh resets the throttle clock; backfill-only passes don't
    # (else the every-fire backfill would keep the refresh from ever running).
    if rc == 0 and do_refresh:
        _record_success()
    return rc


def _run_pipeline(args, start_year, end_year, years, since, do_refresh) -> int:
    results: list = []

    if do_refresh:
        # 1. Clear the recent year-window cache so no stale pages survive the re-pull.
        for d in _window_dirs(years):
            if d.exists():
                shutil.rmtree(d)
                print(f"[tick] cleared stale cache {d.relative_to(ROOT)}")

        # 2. Re-pull recent search windows (fresh statuses + newly-filed permits).
        if run_step(
            "step0: re-pull recent search windows",
            [str(SRC / "step0_fetch_search_results.py"), "--module", MODULE,
             "--start-year", str(start_year), "--end-year", str(end_year),
             "--no-cache", "--page-delay", str(args.page_delay)],
            critical=True, results=results) != 0:
            return _summary(results, 2)

        # 3. Parse search -> sca_permits (idempotent; refreshes status/dates).
        if run_step(
            "step1: parse search -> sca_permits",
            [str(SRC / "step1_parse_search_results.py"), "--module", MODULE],
            critical=True, results=results) != 0:
            return _summary(results, 2)

        # 4. Fetch detail for NEW records in the window (cache-skip = only the new).
        run_step(
            "step2: fetch detail for new records",
            [str(SRC / "step2_fetch_details.py"), "--module", MODULE,
             "--since", since, "--page-delay", str(args.detail_delay)],
            critical=False, results=results)

    # 4b. Progressive historical backfill: enrich a chunk of the oldest-missing
    #     records every fire (newest-first) until the whole history has detail.
    if args.backfill_chunk > 0:
        run_step(
            f"step2: backfill historical detail (newest {args.backfill_chunk} missing)",
            [str(SRC / "step2_fetch_details.py"), "--module", MODULE,
             "--missing-detail", "--limit", str(args.backfill_chunk),
             "--page-delay", str(args.detail_delay)],
            critical=False, results=results)

    # 5. Parse detail -> detail + contacts. --missing-only keeps this O(new) by
    #    parsing just the freshly-fetched files, not the whole growing cache each
    #    fire (a full re-parse stays a manual no-flag run after a parser change).
    run_step(
        "step2: parse detail (incremental)",
        [str(SRC / "step2_parse_details.py"), "--module", MODULE, "--missing-only"],
        critical=False, results=results)

    # 6. Re-score everything (cheap; new permits + changed statuses).
    if run_step(
        "step3: score -> sca_leads",
        [str(SRC / "step3_score.py"), "--rebuild"],
        critical=True, results=results) != 0:
        return _summary(results, 2)

    # 6b. Re-cluster scored leads into one-row-per-project (dedupe the call list).
    run_step(
        "step3b: cluster leads -> projects",
        [str(SRC / "step3b_cluster.py"), "--module", MODULE],
        critical=False, results=results)

    # 7. Regenerate D1 sync — both per-permit leads and the deduped project list.
    #    (push only when explicitly asked + creds present.)
    if not args.skip_sync:
        # --prune keeps the generated SQL a true mirror: leads that fell out of
        # the actionable set (issued/completed -> DROP) are removed, not left
        # stale. Safe here -- the tick exports the full set (no --limit).
        for label, extra in (("leads", []), ("project clusters", ["--clusters"])):
            sync_args = [str(SRC / "step4_sync_d1.py"), *extra, "--prune"]
            if args.execute_sync:
                sync_args.append("--execute")
            run_step(f"step4: sync {label} to D1", sync_args,
                     critical=False, results=results)

    # 8. Healthcheck — verify integrity before anyone reads the refreshed data.
    run_step("healthcheck: verify DB integrity",
             [str(SRC / "healthcheck.py"), "-q"], critical=False, results=results)

    return _summary(results, 0)


def _summary(results: list, rc: int) -> int:
    print(f"\n{'='*70}\n[tick] summary\n{'='*70}")
    for label, r in results:
        print(f"  {'ok ' if r == 0 else 'ERR'} ({r})  {label}")
    print(f"[tick] {'completed' if rc == 0 else 'ABORTED'} (exit {rc})")
    return rc


if __name__ == "__main__":
    p = argparse.ArgumentParser(description="Incremental tick for the SC lead funnel.")
    p.add_argument("--refresh-years", type=int, default=2,
                   help="How many recent ApplyDate years to re-pull (default 2).")
    p.add_argument("--page-delay", type=float, default=1.0,
                   help="Seconds between search pages (default 1.0).")
    p.add_argument("--detail-delay", type=float, default=0.3,
                   help="Seconds between detail GETs (default 0.3).")
    p.add_argument("--backfill-chunk", type=int, default=200,
                   help="Historical-detail records to enrich per fire (newest "
                        "missing first), separate from the throttled refresh; "
                        "runs every tick until the ~50k history is complete. "
                        "0 disables (default 200).")
    p.add_argument("--skip-sync", action="store_true",
                   help="Don't run step 4 (no D1 SQL regeneration).")
    p.add_argument("--execute-sync", action="store_true",
                   help="Pass --execute to step 4 (push to D1; needs CF_* env).")
    p.add_argument("--dry-run", action="store_true",
                   help="Print the plan and exit; touch nothing.")
    p.add_argument("--min-interval-hours", type=float, default=6.0,
                   help="Skip if a successful tick ran within this many hours "
                        "(default 6) — keeps frequent schedulers off the portal.")
    p.add_argument("--force", action="store_true",
                   help="Bypass the min-interval throttle and run anyway.")
    raise SystemExit(main(p.parse_args()))

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

Anonymous, read-only, single-IP — same posture as a manual run; this only
automates the sequence. Scheduling it (cron) is the user's call; see README.

    python3 src/tick.py                  # default: refresh last 2 years
    python3 src/tick.py --refresh-years 1
    python3 src/tick.py --dry-run        # print the plan, touch nothing
    python3 src/tick.py --execute-sync   # also push to D1 (needs CF_* env)
"""

from __future__ import annotations

import argparse
import datetime as dt
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"
PY = sys.executable
MODULE = "Permit"
RAW_SUBDIR = "permit"  # MODULES["Permit"]["raw_subdir"]


def _window_dirs(years: list[int]) -> list[Path]:
    base = ROOT / "outputs" / "raw" / "sca" / RAW_SUBDIR
    return [base / str(y) for y in years]


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

    if args.dry_run:
        print("\n[tick] DRY RUN — plan only, nothing fetched/cleared/written:")
        print(f"  1. clear cache dirs: {[str(d.relative_to(ROOT)) for d in _window_dirs(years)]}")
        print(f"  2. step0 --start-year {start_year} --end-year {today.year} --no-cache")
        print(f"  3. step1 parse")
        print(f"  4. step2 fetch --since {since}   (cache-skips existing -> new only)")
        print(f"  5. step2 parse")
        print(f"  6. step3 score --rebuild")
        print(f"  7. step4 sync {sync_desc}")
        return 0

    # 1. Clear the recent year-window cache so no stale pages survive the re-pull.
    for d in _window_dirs(years):
        if d.exists():
            shutil.rmtree(d)
            print(f"[tick] cleared stale cache {d.relative_to(ROOT)}")

    results: list = []

    # 2. Re-pull recent search windows (fresh statuses + newly-filed permits).
    if run_step(
        "step0: re-pull recent search windows",
        [str(SRC / "step0_fetch_search_results.py"), "--module", MODULE,
         "--start-year", str(start_year), "--end-year", str(today.year),
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

    # 5. Parse detail -> detail + contacts.
    run_step(
        "step2: parse detail",
        [str(SRC / "step2_parse_details.py"), "--module", MODULE],
        critical=False, results=results)

    # 6. Re-score everything (cheap; new permits + changed statuses).
    if run_step(
        "step3: score -> sca_leads",
        [str(SRC / "step3_score.py"), "--rebuild"],
        critical=True, results=results) != 0:
        return _summary(results, 2)

    # 7. Regenerate D1 sync (push only when explicitly asked + creds present).
    if not args.skip_sync:
        sync_args = [str(SRC / "step4_sync_d1.py")]
        if args.execute_sync:
            sync_args.append("--execute")
        run_step("step4: sync to D1", sync_args, critical=False, results=results)

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
    p.add_argument("--skip-sync", action="store_true",
                   help="Don't run step 4 (no D1 SQL regeneration).")
    p.add_argument("--execute-sync", action="store_true",
                   help="Pass --execute to step 4 (push to D1; needs CF_* env).")
    p.add_argument("--dry-run", action="store_true",
                   help="Print the plan and exit; touch nothing.")
    raise SystemExit(main(p.parse_args()))

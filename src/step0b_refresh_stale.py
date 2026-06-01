"""Step 0b: targeted re-pull of stale-status actionable leads.

The tick's main refresh only re-pulls the last few ApplyDate years (default 2),
so a lead filed before that window stops getting status updates: a 2024 permit
frozen at "Approved" that the city has since Issued/Finaled lingers on the GC's
call list as actionable when it may be dead. ~27% of the actionable funnel can be
outside the window at any time.

This closes that gap cheaply. Status — the score-critical field — lives on the
SEARCH row, and the search API filters by ApplyDate, so we re-pull the narrow
ApplyDate DAY-window of every currently-actionable (HIGH/MEDIUM) lead whose
apply_date is older than the refresh floor. One day-window per distinct at-risk
filing day (San Carlos files only a handful of permits/day), cached like any other
window — so the existing step1 re-parses them and upserts the fresh status, and
step3's next re-score drops any that went dead. The day-window label
("YYYYMMDD-YYYYMMDD") sorts AFTER the bare-year window ("YYYY"), so step1 parses
the fresh page last and it wins over the stale year-window page.

    python3 src/step0b_refresh_stale.py                 # refresh-years 2 (tick default)
    python3 src/step0b_refresh_stale.py --refresh-years 3
    python3 src/step0b_refresh_stale.py --dry-run       # list the at-risk days, fetch nothing

Anonymous, read-only — same posture as step0; this just targets a small set of
days the main window skips. Outputs cache pages (consumed by step1) + an audit
ledger.
"""

from __future__ import annotations

import argparse
import datetime as dt
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from utils.config import DEFAULT_PAGE_SIZE, MODULES
from utils.io import ROOT, atomic_write_json, connect, load_run_ledger
from utils.step_0.fetch import SearchError, _fetch_window, make_session

OUTPUTS_DIR = ROOT / "outputs" / "step_0"


def raw_dir_for(module: str) -> Path:
    return ROOT / "outputs" / "raw" / "sca" / MODULES[module]["raw_subdir"]


def runs_json_for(module: str) -> Path:
    return OUTPUTS_DIR / f"stale_runs_{MODULES[module]['raw_subdir']}.json"


def refresh_floor(today: dt.date, refresh_years: int) -> str:
    """ISO date: leads with apply_date < this are OUTSIDE the main refresh window
    (so their status goes stale). Mirrors the tick's window: refresh_years=2,
    today 2026 -> '2025-01-01' (the 2025+2026 windows are refreshed; <2025 isn't)."""
    return f"{today.year - refresh_years + 1}-01-01"


# Stop the day-window loop after this many CONSECUTIVE portal errors. A burst of
# errors means the portal is rate-limiting/blocking (observed 2026-06-01: anonymous
# search 403'd under cumulative daily load), so grinding through the remaining days
# would just hammer an active block and deepen it. Bail politely; the unrefreshed
# days simply wait for a future fire.
MAX_CONSECUTIVE_ERRORS = 3


def stale_apply_days(apply_dates) -> list[dt.date]:
    """Distinct ApplyDate calendar days (sorted) from the at-risk leads' apply_date
    strings. Skips blank/unparseable values (a lead with no apply_date isn't
    window-scoped, so there's no day to re-pull). Dedup means many leads filed the
    same day cost ONE day-window."""
    days = set()
    for ad in apply_dates:
        if not ad:
            continue
        try:
            days.add(dt.date.fromisoformat(ad[:10]))
        except (ValueError, TypeError):
            continue
    return sorted(days)


def fetch_day_windows(days, fetch_one_day, log=print, sleep=time.sleep,
                      delay=0.0) -> dict:
    """Fetch each day-window via fetch_one_day(day), stopping early after
    MAX_CONSECUTIVE_ERRORS consecutive failures (the portal is rate-limiting, so
    further requests just hammer an active block). fetch_one_day raises SearchError
    on failure. Pure control loop (fetch + sleep injected) so the early-abort is
    unit-tested without a portal. Returns {attempted, errors, aborted}."""
    consecutive = 0
    errors = []
    aborted = False
    attempted = 0
    for d in days:
        attempted += 1
        try:
            fetch_one_day(d)
            consecutive = 0
        except SearchError as exc:
            errors.append({"day": d.isoformat(), "error": str(exc)})
            consecutive += 1
            log(f"  [{d.isoformat()}] ERROR: {exc}")
            if consecutive >= MAX_CONSECUTIVE_ERRORS:
                aborted = True
                log(f"  aborting: {consecutive} consecutive portal errors "
                    f"(rate-limited?); remaining day-windows deferred to a "
                    f"future fire.")
                break
        sleep(delay)
    return {"attempted": attempted, "errors": errors, "aborted": aborted}


def _at_risk_apply_dates(conn, floor: str) -> list[str]:
    """apply_date of every actionable (HIGH/MEDIUM) lead filed before the floor --
    the ones whose status the main window no longer refreshes. Returns [] if the
    leads table doesn't exist yet (brand-new DB)."""
    try:
        rows = conn.execute(
            "SELECT p.apply_date FROM sca_leads l JOIN sca_permits p USING(case_id) "
            "WHERE l.lead_band IN ('HIGH','MEDIUM') AND p.apply_date IS NOT NULL "
            "AND p.apply_date < ?", (floor,)).fetchall()
    except sqlite3.OperationalError:
        return []
    return [r[0] for r in rows]


def main(args) -> int:
    module = args.module
    if module not in MODULES:
        print(f"[error] unknown module {module!r}", file=sys.stderr)
        return 2

    started = dt.datetime.now().astimezone().replace(microsecond=0)
    run_id = started.strftime("%Y-%m-%d_%H%M%S")
    today = dt.date.today()
    floor = refresh_floor(today, args.refresh_years)

    conn = connect()
    try:
        apply_dates = _at_risk_apply_dates(conn, floor)
    finally:
        conn.close()

    days = stale_apply_days(apply_dates)
    capped = False
    if args.limit_days and len(days) > args.limit_days:
        # Bound portal load: refresh the most RECENT at-risk days first (a 2024
        # lead is likelier still-relevant than a 2021 one). Older days simply wait
        # for a future fire -- they're already low-recency.
        capped = True
        days = days[-args.limit_days:]

    print(f"[{run_id}] module:   {module}")
    print(f"[{run_id}] floor:    {floor}  (actionable leads filed before this are "
          f"outside the {args.refresh_years}y refresh window)")
    print(f"[{run_id}] at-risk:  {len(apply_dates)} old actionable leads across "
          f"{len(days)} distinct filing day(s)"
          + (f" (capped to newest {args.limit_days})" if capped else ""))

    if not days:
        print("  nothing to refresh — no actionable leads outside the window.")
        return 0

    if args.dry_run:
        print("  DRY RUN — day-windows that WOULD be re-pulled:")
        for d in days:
            print(f"    {d.isoformat()}")
        return 0

    raw_dir = raw_dir_for(module)
    sort_by = MODULES[module]["sort_by"]
    filter_module = MODULES[module]["filter_module"]
    session = make_session()
    audit = {
        "at_risk_leads": len(apply_dates), "days": len(days), "capped": capped,
        "floor": floor, "windows": [], "sum_window_counts": 0,
        "pages_fetched": 0, "pages_skipped": 0, "records_seen": 0, "errors": [],
    }
    def fetch_one_day(d):
        # no_cache=True: we WANT fresh statuses, overwriting any prior day-window
        # pages for this date. _fetch_window records its own audit fields.
        _fetch_window(session, filter_module, sort_by, d, d, raw_dir,
                      DEFAULT_PAGE_SIZE, args.page_delay, True, audit, print)

    try:
        outcome = fetch_day_windows(days, fetch_one_day, log=print,
                                    sleep=time.sleep, delay=args.page_delay)
    finally:
        session.close()
    audit["errors"].extend(outcome["errors"])
    audit["aborted_on_errors"] = outcome["aborted"]

    finished = dt.datetime.now().astimezone().replace(microsecond=0)
    print(f"  re-pulled {audit['pages_fetched']} page(s) across {len(days)} day-window(s); "
          f"{audit['records_seen']} records refreshed (step1 will upsert fresh status).")

    runs = load_run_ledger(runs_json_for(module))
    runs["runs"].append({
        "run_id": run_id, "started_at": started.isoformat(),
        "finished_at": finished.isoformat(), **audit,
    })
    atomic_write_json(runs_json_for(module), runs)
    print(f"  ledger:   {runs_json_for(module).relative_to(ROOT)}")
    return 1 if audit["errors"] else 0


if __name__ == "__main__":
    p = argparse.ArgumentParser(
        description="Step 0b: re-pull stale-status actionable leads outside the "
                    "main refresh window (by their ApplyDate day-window).")
    p.add_argument("--module", default="Permit", choices=sorted(MODULES))
    p.add_argument("--refresh-years", type=int, default=2,
                   help="Match the tick's window: leads filed before "
                        "(current_year - refresh_years + 1) are refreshed here "
                        "(default 2).")
    p.add_argument("--page-delay", type=float, default=1.0,
                   help="Seconds between day-window requests (default 1.0).")
    p.add_argument("--limit-days", type=int, default=120,
                   help="Cap the number of day-windows per run (newest first) so a "
                        "large backlog can't hammer the portal (default 120).")
    p.add_argument("--dry-run", action="store_true",
                   help="List the at-risk day-windows without fetching.")
    raise SystemExit(main(p.parse_args()))

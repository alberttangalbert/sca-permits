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
    of per-record GETs for the oldest-not-yet-enriched permits, newest-first, down
    to the --backfill-since floor (default 2020-01-01: the pre-2020 tail is all
    >5y old -> recency_factor 0.1 -> never an actionable lead, so it's skipped).
    Once everything on/after the floor has detail it self-quiesces (nothing
    missing -> nothing fetched), and a fully-throttled, fully-backfilled tick
    skips cheaply without taking the lock.

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
from utils.io import DB_PATH, atomic_write_json, connect  # noqa: E402  (after SRC is on the path)

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


# If a refresh fails (e.g. portal sustained 500), don't keep hammering the
# API every fire -- each failed attempt costs ~3 min of retry timeouts.
# Wait at least this long before trying again. Bounded so a brief outage
# self-heals on the next scheduled fire.
OUTAGE_BACKOFF_MINUTES = 30


def _read_state() -> dict:
    try:
        return json.loads(STATE_PATH.read_text())
    except (OSError, ValueError):
        return {}


def _last_success() -> dt.datetime | None:
    try:
        return dt.datetime.fromisoformat(_read_state()["last_success_at"])
    except (ValueError, KeyError):
        return None


def _last_failure() -> dt.datetime | None:
    try:
        return dt.datetime.fromisoformat(_read_state()["last_failure_at"])
    except (ValueError, KeyError):
        return None


def _record_success() -> None:
    # Atomic write: a kill mid-write would otherwise leave STATE_PATH as a
    # partial JSON blob. _last_success() catches that as ValueError and returns
    # None (graceful: triggers an unscheduled extra refresh next tick), but
    # writing via the tmp+rename helper avoids the broken state entirely.
    # Also clears last_failure_at so a recovered portal exits outage-backoff.
    now = dt.datetime.now().astimezone().replace(microsecond=0)
    state = _read_state()
    state["last_success_at"] = now.isoformat()
    state.pop("last_failure_at", None)
    atomic_write_json(STATE_PATH, state)


def _record_failure() -> None:
    now = dt.datetime.now().astimezone().replace(microsecond=0)
    state = _read_state()
    state["last_failure_at"] = now.isoformat()
    atomic_write_json(STATE_PATH, state)


def _missing_detail_count(since: str | None = None) -> int:
    """Permits with no parsed detail row yet — the historical backfill remaining.
    `since` (ISO date) applies the same apply_date floor the backfill itself uses,
    so the skip/quiesce logic counts only records the backfill will actually act
    on (e.g. with a 2020 floor, the pre-2020 tail isn't counted as 'work to do')."""
    if not DB_PATH.exists():
        return 0
    sql = ("SELECT COUNT(*) FROM sca_permits p WHERE NOT EXISTS "
           "(SELECT 1 FROM sca_permit_detail d WHERE d.case_id = p.case_id)")
    params: list = []
    if since:
        sql += " AND p.apply_date >= ?"
        params.append(since)
    conn = connect()
    try:
        return conn.execute(sql, params).fetchone()[0]
    except Exception:
        return 0  # tables not created yet — a real refresh will build them
    finally:
        conn.close()


def should_refresh(force: bool, last: dt.datetime | None, now: dt.datetime,
                   min_interval_hours: float,
                   last_failure: dt.datetime | None = None,
                   outage_backoff_minutes: float = OUTAGE_BACKOFF_MINUTES) -> bool:
    """Whether to run the heavy SEARCH re-pull this fire: forced, or no prior
    successful run, or the throttle window has elapsed. Pure (no I/O) so the
    two-cadence gate — the pipeline's one portal-politeness decision — is tested
    rather than trusted to `or` short-circuiting around a None age.

    Outage backoff: if the last refresh attempt FAILED within
    outage_backoff_minutes (default 30), skip this fire's refresh. Each failed
    refresh costs ~3 min of retry timeouts; the backoff prevents the tick from
    burning ~30% of its wall-clock on a sustained portal outage. `force=True`
    overrides backoff (operator decision wins)."""
    if force:
        return True
    if (last_failure is not None
            and (now - last_failure).total_seconds() / 60 < outage_backoff_minutes):
        return False
    if last is None:
        return True
    return (now - last).total_seconds() / 3600 >= min_interval_hours


def should_skip_entirely(do_refresh: bool, backfill_chunk: int,
                         missing: int) -> bool:
    """Nothing to do at all (so the tick can return without taking the lock):
    the refresh is throttled AND the backfill is disabled or already complete."""
    return (not do_refresh) and (backfill_chunk <= 0 or missing <= 0)


def refresh_state_action(fetch_ok: bool | None) -> str:
    """Decide what to persist to the refresh state file after a tick, keyed on
    the NETWORK pull (step0) ONLY — never on a downstream LOCAL step.

    `fetch_ok` is the step0 outcome: None = no refresh attempted (backfill-only
    or throttled fire), True = the portal pull succeeded, False = it failed.

    Two correctness reasons it must ignore the overall pipeline rc:
      * A local step3 (scoring) / step1 (parse) failure must NOT masquerade as a
        portal outage — recording failure there would wrongly suppress the next
        search refresh for OUTAGE_BACKOFF_MINUTES even though the portal is fine.
      * A successful portal pull must reset the throttle clock even if a later
        local step failed; otherwise a persistent local bug (last_success never
        updated) would re-pull the live portal on every single fire.
    Returns one of: "record_success", "record_failure", "none"."""
    if fetch_ok is True:
        return "record_success"
    if fetch_ok is False:
        return "record_failure"
    return "none"


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
    last_failure = _last_failure()
    now = dt.datetime.now().astimezone()
    age_h = (now - last).total_seconds() / 3600 if last else None
    failure_age_min = ((now - last_failure).total_seconds() / 60
                       if last_failure else None)
    do_refresh = should_refresh(args.force, last, now, args.min_interval_hours,
                                last_failure)
    in_outage_backoff = (not args.force and last_failure is not None
                         and failure_age_min is not None
                         and failure_age_min < OUTAGE_BACKOFF_MINUTES)

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
        floor = f" --since {args.backfill_since}" if args.backfill_since else ""
        print(f"  4b. step2 backfill --missing-detail{floor} --limit {args.backfill_chunk}"
              f"   ({_missing_detail_count(args.backfill_since)} records still lack detail"
              + (f", apply_date >= {args.backfill_since}" if args.backfill_since else "")
              + ")")
        print(f"  5. step2 parse --missing-only (incremental)")
        print(f"  6. step3 score --rebuild")
        print(f"  6b. step3b cluster leads -> projects")
        print(f"  7. step4 sync leads + clusters {sync_desc}")
        print(f"  8. healthcheck verify DB integrity")
        return 0

    # Nothing to do only when the refresh is throttled AND the backfill is done
    # (or disabled): then we skip cheaply without even taking the lock. The
    # --backfill-since floor scopes "done" to the records we actually enrich
    # (default: skip the pre-2020 archival tail — it's never an actionable lead).
    missing = _missing_detail_count(args.backfill_since)
    if should_skip_entirely(do_refresh, args.backfill_chunk, missing):
        if in_outage_backoff:
            print(f"[tick] refresh in outage backoff (last failure {failure_age_min:.1f}min "
                  f"ago < {OUTAGE_BACKOFF_MINUTES}min) and backfill complete "
                  f"(missing detail: {missing}); nothing to do.")
        else:
            print(f"[tick] refresh throttled ({age_h:.1f}h < {args.min_interval_hours}h) "
                  f"and backfill complete (missing detail: {missing}); nothing to do.")
        return 0
    if not do_refresh:
        if in_outage_backoff:
            print(f"[tick] refresh in outage backoff (last failure {failure_age_min:.1f}min "
                  f"ago < {OUTAGE_BACKOFF_MINUTES}min); backfill-only pass — "
                  f"{missing} records still lack detail.")
        else:
            print(f"[tick] refresh throttled ({age_h:.1f}h < {args.min_interval_hours}h); "
                  f"backfill-only pass — {missing} records still lack detail.")

    # Single-flight: refuse to run while another tick holds the lock.
    if not _acquire_lock():
        return 0
    try:
        rc, fetch_ok = _run_pipeline(args, start_year, today.year, years, since,
                                     do_refresh)
    finally:
        _release_lock()
    # Refresh state (throttle clock + outage backoff) is keyed on the NETWORK
    # pull (step0) ONLY, via fetch_ok — never on the overall rc, so a downstream
    # LOCAL step3/step1 failure can't masquerade as a portal outage and a
    # successful pull still resets the throttle. See refresh_state_action.
    action = refresh_state_action(fetch_ok)
    if action == "record_success":
        _record_success()
    elif action == "record_failure":
        # Portal fetch failed: next fire enters outage backoff so it doesn't
        # burn another ~3 min of retry timeouts on a still-down portal.
        _record_failure()
    return rc


def _run_pipeline(args, start_year, end_year, years, since,
                  do_refresh) -> tuple[int, bool | None]:
    results: list = []
    # Step0 (network pull) outcome, used by refresh_state_action: None = not
    # attempted, True = pulled OK, False = the portal fetch failed.
    fetch_ok: bool | None = None

    if do_refresh:
        # 1. Re-pull recent search windows (fresh statuses + newly-filed permits).
        #    --no-cache overwrites each page in place via atomic_write_json
        #    (write-tmp + rename), so the OLD cache stays valid until each new
        #    page lands. Earlier design rm-treed the dir up front; the portal
        #    going 500 mid-day (2026-05-30 03:10 local) then stranded the
        #    cache empty for ~3 ticks until the API recovered. In practice the
        #    page-count grows monotonically (permits only get ADDED to
        #    EnerGov), so leaving old pages in place can't corrupt step 1 --
        #    its INSERT ON CONFLICT dedup means any page-N record present in
        #    both the new and the old fetch resolves to the LATER write
        #    (filenames sort by number, so growing pages don't shadow). Stale
        #    pages that only exist in the old cache would only matter if the
        #    page count SHRANK; for a city's incremental permit history that
        #    never happens.
        if run_step(
            "step0: re-pull recent search windows",
            [str(SRC / "step0_fetch_search_results.py"), "--module", MODULE,
             "--start-year", str(start_year), "--end-year", str(end_year),
             "--no-cache", "--page-delay", str(args.page_delay)],
            critical=True, results=results) != 0:
            # Network pull failed -> outage backoff (this is the ONLY refresh
            # failure; step1 below is local parsing, not a portal outage).
            return _summary(results, 2), False
        fetch_ok = True   # portal pull succeeded (network work done)

        # 3. Parse search -> sca_permits (idempotent; refreshes status/dates).
        if run_step(
            "step1: parse search -> sca_permits",
            [str(SRC / "step1_parse_search_results.py"), "--module", MODULE],
            critical=True, results=results) != 0:
            return _summary(results, 2), fetch_ok

        # 4. Fetch detail for NEW records in the window (cache-skip = only the new).
        run_step(
            "step2: fetch detail for new records",
            [str(SRC / "step2_fetch_details.py"), "--module", MODULE,
             "--since", since, "--page-delay", str(args.detail_delay)],
            critical=False, results=results)

    # 4b. Progressive historical backfill: enrich a chunk of the oldest-missing
    #     records every fire (newest-first) until the history-of-interest has
    #     detail. --backfill-since floors how far back we go (default 2020-01-01:
    #     pre-2020 permits are all >5y old -> recency_factor 0.1 -> never an
    #     actionable lead, so enriching them is archival-only, low value).
    if args.backfill_chunk > 0:
        backfill_args = [str(SRC / "step2_fetch_details.py"), "--module", MODULE,
                         "--missing-detail", "--limit", str(args.backfill_chunk),
                         "--page-delay", str(args.detail_delay)]
        if args.backfill_since:
            backfill_args += ["--since", args.backfill_since]
        run_step(
            f"step2: backfill historical detail (newest {args.backfill_chunk} missing"
            + (f", since {args.backfill_since}" if args.backfill_since else "") + ")",
            backfill_args, critical=False, results=results)

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
        # --no-cluster-note: step3b runs immediately below (step 6b), so the
        # standalone "run step3b next" note would be misleading noise here.
        [str(SRC / "step3_score.py"), "--rebuild", "--no-cluster-note"],
        critical=True, results=results) != 0:
        # Local scoring failure: surfaced via rc, but NOT a portal outage --
        # fetch_ok (step0's outcome) is returned unchanged so a successful
        # pull still resets the throttle and a no-refresh fire stays "none".
        return _summary(results, 2), fetch_ok

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

    return _summary(results, 0), fetch_ok


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
                        "runs every tick until the history-of-interest is "
                        "complete. 0 disables (default 200).")
    p.add_argument("--backfill-since", default="2020-01-01",
                   help="Floor (ISO date) on the historical-detail backfill: only "
                        "enrich permits applied on/after this. Default 2020-01-01 "
                        "skips the pre-2020 archival tail (those are all >5y old -> "
                        "recency_factor 0.1 -> never actionable leads). Pass an "
                        "empty string to backfill the full 1999-present history.")
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

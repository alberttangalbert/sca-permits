"""Step 2 (fetch): pull per-record detail JSON from the EnerGov CSS portal.

Selects case_ids from sca_permits (step 1's output), then fetches each record's
detail via one anonymous GET and caches the raw JSON for the step-2 parser.

Selection is filterable so you can enrich a high-value slice instead of the full
52k history — recent + relevant records are where the lead value is:

    # the fresh-leads window (recent years), polite pacing:
    python3 src/step2_fetch_details.py --start-year 2025

    # everything applied on/after a date:
    python3 src/step2_fetch_details.py --since 2024-01-01

    # a quick smoke test:
    python3 src/step2_fetch_details.py --start-year 2026 --limit 25

    # the full historical backfill (~52k GETs — long):
    python3 src/step2_fetch_details.py --all

Outputs:
    outputs/raw/sca/permit_detail/<case_id>.json   (cached detail, per record)
    outputs/step_2/fetch_runs_<module>.json        (audit log)
"""

from __future__ import annotations

import argparse
import datetime as dt
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from utils.config import MODULES
from utils.io import ROOT, atomic_write_json, connect, load_json
from utils.step_2.detail import fetch_details

OUTPUTS_DIR = ROOT / "outputs" / "step_2"


def raw_dir_for(module: str) -> Path:
    return ROOT / "outputs" / "raw" / "sca" / f"{MODULES[module]['raw_subdir']}_detail"


def fetch_runs_json_for(module: str) -> Path:
    return OUTPUTS_DIR / f"fetch_runs_{MODULES[module]['raw_subdir']}.json"


def select_case_ids(conn, module: str, start_year: int | None, end_year: int | None,
                    since: str | None, statuses: list[str] | None,
                    type_like: str | None, limit: int | None,
                    missing_detail: bool = False) -> list[str]:
    where = ["module = ?"]
    params: list = [module]
    # apply_date is an ISO string; year/date prefix comparisons work lexically.
    if start_year is not None:
        where.append("apply_date >= ?"); params.append(f"{start_year}-01-01")
    if end_year is not None:
        where.append("apply_date <= ?"); params.append(f"{end_year}-12-31T23:59:59")
    if since:
        where.append("apply_date >= ?"); params.append(since)
    if statuses:
        where.append(f"case_status IN ({', '.join('?' for _ in statuses)})")
        params.extend(statuses)
    if type_like:
        where.append("case_type LIKE ?"); params.append(f"%{type_like}%")
    if missing_detail:
        # Only records not yet enriched — drives the progressive history backfill.
        where.append("NOT EXISTS (SELECT 1 FROM sca_permit_detail d "
                     "WHERE d.case_id = sca_permits.case_id)")
    # Newest first: if you only enrich a slice, the most recent (most relevant)
    # records win.
    sql = (f"SELECT case_id FROM sca_permits WHERE {' AND '.join(where)} "
           f"ORDER BY apply_date DESC")
    if limit:
        sql += " LIMIT ?"; params.append(limit)
    return [r[0] for r in conn.execute(sql, params).fetchall()]


def main(args) -> int:
    module = args.module
    if module not in MODULES:
        print(f"[error] unknown module {module!r}", file=sys.stderr)
        return 2
    if not any([args.all, args.start_year, args.end_year, args.since,
                args.status, args.type_like, args.limit, args.missing_detail]):
        print("[error] refusing to select ALL records implicitly; pass --all "
              "or a filter (--start-year / --since / --status / --limit / "
              "--missing-detail).", file=sys.stderr)
        return 2

    started = dt.datetime.now().astimezone().replace(microsecond=0)
    run_id = started.strftime("%Y-%m-%d_%H%M%S")
    statuses = [s.strip() for s in args.status.split(",")] if args.status else None

    conn = connect()
    try:
        case_ids = select_case_ids(
            conn, module, args.start_year, args.end_year, args.since,
            statuses, args.type_like, args.limit, args.missing_detail)
    finally:
        conn.close()

    raw_dir = raw_dir_for(module)
    print(f"[{run_id}] module:    {module}")
    print(f"[{run_id}] selected:  {len(case_ids)} case_ids")
    print(f"[{run_id}] cache dir: {raw_dir.relative_to(ROOT)}")
    print(f"[{run_id}] pacing:    {args.page_delay}s between GETs"
          + ("  (no-cache: refetching)" if args.no_cache else ""))
    if not case_ids:
        print("  nothing to fetch.")
        return 0

    audit = fetch_details(case_ids, raw_dir, page_delay=args.page_delay,
                          no_cache=args.no_cache)

    finished = dt.datetime.now().astimezone().replace(microsecond=0)
    print()
    print(f"[{run_id}] complete in {audit['duration_seconds']}s")
    print(f"  requested: {audit['requested']}")
    print(f"  fetched:   {audit['fetched']}")
    print(f"  skipped:   {audit['skipped']} (already cached)")
    print(f"  errors:    {len(audit['errors'])}")

    runs = load_json(fetch_runs_json_for(module), {"schema_version": 1, "runs": []})
    runs["runs"].append({
        "run_id": run_id,
        "started_at": started.isoformat(),
        "finished_at": finished.isoformat(),
        "selection": {
            "start_year": args.start_year, "end_year": args.end_year,
            "since": args.since, "status": args.status,
            "type_like": args.type_like, "limit": args.limit, "all": args.all,
            "missing_detail": args.missing_detail,
        },
        **audit,
    })
    atomic_write_json(fetch_runs_json_for(module), runs)
    print(f"  ledger:    {fetch_runs_json_for(module).relative_to(ROOT)}")
    return 1 if audit["errors"] else 0


if __name__ == "__main__":
    p = argparse.ArgumentParser(
        description="Step 2 (fetch): cache EnerGov per-record detail JSON.")
    p.add_argument("--module", default="Permit", choices=sorted(MODULES))
    p.add_argument("--all", action="store_true",
                   help="Select every record for the module (full backfill).")
    p.add_argument("--start-year", type=int,
                   help="Only records with apply_date year >= this.")
    p.add_argument("--end-year", type=int,
                   help="Only records with apply_date year <= this.")
    p.add_argument("--since", help="Only records with apply_date >= this (ISO date).")
    p.add_argument("--status", help="Comma-separated case_status filter "
                                    "(e.g. 'Issued,Approved,In Review').")
    p.add_argument("--type-like", help="SQL LIKE fragment on case_type "
                                       "(e.g. 'Residential').")
    p.add_argument("--missing-detail", action="store_true",
                   help="Only records that lack a parsed detail row (drives the "
                        "progressive historical backfill; combine with --limit).")
    p.add_argument("--limit", type=int, help="Cap the number of records.")
    p.add_argument("--page-delay", type=float, default=0.3,
                   help="Seconds between GETs (default 0.3).")
    p.add_argument("--no-cache", action="store_true",
                   help="Refetch records even if already cached.")
    raise SystemExit(main(p.parse_args()))

"""Step 3 (score): rank enriched permits into banded leads -> sca_leads.

Joins each enriched record (sca_permits + sca_permit_detail) with its contacts,
applies the gates×factors model (src/utils/step_3/scoring.py), and upserts one
sca_leads row. Only records that have a detail row (step 2 ran on them) can be
scored — valuation and contacts come from there.

Scoring is cheap and idempotent, so re-run it freely after tuning the rules:

    python3 src/step3_score.py                 # score everything enriched
    python3 src/step3_score.py --rebuild       # wipe sca_leads first, then score
    python3 src/step3_score.py --since 2025-01-01
    python3 src/step3_score.py --dry-run        # report distribution, no writes

Outputs:
    sca_leads rows in outputs/sca_permits.db
    outputs/step_3/score_runs_<module>.json     (audit log)
"""

from __future__ import annotations

import argparse
import datetime as dt
import sys
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from utils.config import MODULES
from utils.io import ROOT, atomic_write_json, connect, load_json
from utils.step_3.scoring import score_record

OUTPUTS_DIR = ROOT / "outputs" / "step_3"

LEAD_COLUMNS = [
    "case_id", "lead_score", "lead_band", "category",
    "type_fit", "size_factor", "status_factor", "contractor_factor",
    "recency_factor", "status_bucket", "valuation", "has_contractor",
    "owner_name", "owner_email", "owner_phone", "contact_role",
    "contractor_name",
    "additional_sqft", "num_stories", "construction_type", "blocking_hold",
]


def score_runs_json_for(module: str) -> Path:
    return OUTPUTS_DIR / f"score_runs_{MODULES[module]['raw_subdir']}.json"


def _upsert_sql() -> str:
    cols = LEAD_COLUMNS + ["scored_at"]
    placeholders = ", ".join(f":{c}" for c in cols)
    updates = ", ".join(f"{c}=excluded.{c}" for c in cols if c != "case_id")
    return (f"INSERT INTO sca_leads ({', '.join(cols)}) "
            f"VALUES ({placeholders}) "
            f"ON CONFLICT(case_id) DO UPDATE SET {updates}")


def _load_permits(conn, module, since, start_year, end_year, limit):
    where = ["p.module = ?"]
    params: list = [module]
    if start_year is not None:
        where.append("p.apply_date >= ?"); params.append(f"{start_year}-01-01")
    if end_year is not None:
        where.append("p.apply_date <= ?"); params.append(f"{end_year}-12-31T23:59:59")
    if since:
        where.append("p.apply_date >= ?"); params.append(since)
    sql = (f"SELECT p.case_id, p.case_type, p.case_status, p.description, d.valuation, "
           f"d.additional_sqft, d.num_stories, d.construction_type, d.blocking_hold_count, "
           f"p.apply_date "
           f"FROM sca_permit_detail d JOIN sca_permits p USING(case_id) "
           f"WHERE {' AND '.join(where)} ORDER BY p.apply_date DESC")
    if limit:
        sql += " LIMIT ?"; params.append(limit)
    return conn.execute(sql, params).fetchall()


def _load_contacts(conn, case_ids: set[str]) -> dict[str, list[dict]]:
    by_case: dict[str, list[dict]] = defaultdict(list)
    cur = conn.execute(
        "SELECT case_id, role, full_name, company, email, phone "
        "FROM sca_permit_contacts")
    for case_id, role, full_name, company, email, phone in cur:
        if case_id in case_ids:
            by_case[case_id].append({
                "role": role, "full_name": full_name, "company": company,
                "email": email, "phone": phone})
    return by_case


def main(args) -> int:
    module = args.module
    if module not in MODULES:
        print(f"[error] unknown module {module!r}", file=sys.stderr)
        return 2

    started = dt.datetime.now().astimezone().replace(microsecond=0)
    run_id = started.strftime("%Y-%m-%d_%H%M%S")
    now_iso = started.isoformat()

    conn = connect()
    try:
        permits = _load_permits(conn, module, args.since, args.start_year,
                                args.end_year, args.limit)
        print(f"[{run_id}] module:   {module}")
        print(f"[{run_id}] scoring:  {len(permits)} enriched records")
        if not permits:
            print("  nothing to score — run step2 first (need detail rows).")
            return 1

        case_ids = {row[0] for row in permits}
        contacts_by_case = _load_contacts(conn, case_ids)

        rows = []
        for (case_id, case_type, case_status, description, valuation,
             additional_sqft, num_stories, construction_type, blocking_holds,
             apply_date) in permits:
            lead = score_record(
                case_type, case_status, description, valuation,
                contacts_by_case.get(case_id, []),
                additional_sqft=additional_sqft, num_stories=num_stories,
                construction_type=construction_type,
                blocking_hold_count=blocking_holds, apply_date=apply_date)
            lead["case_id"] = case_id
            rows.append(lead)

        bands = Counter(r["lead_band"] for r in rows)
        cats = Counter(r["category"] for r in rows)
        print(f"  bands:    " + "  ".join(f"{b}={bands.get(b,0)}"
              for b in ("HIGH", "MEDIUM", "LOW", "DROP")))
        print(f"  category: " + "  ".join(f"{c}={n}" for c, n in cats.most_common()))

        if args.dry_run:
            print("  DRY RUN — not writing to DB. Top 10 by score:")
            for r in sorted(rows, key=lambda x: x["lead_score"], reverse=True)[:10]:
                print(f"    {r['lead_score']:5.1f} {r['lead_band']:6} {r['category']:10} "
                      f"val={r['valuation']} {r['status_bucket']}")
            return 0

        upsert = _upsert_sql()
        if args.rebuild:
            conn.execute("DELETE FROM sca_leads")
        before = conn.execute("SELECT COUNT(*) FROM sca_leads").fetchone()[0]
        for r in rows:
            r["scored_at"] = now_iso
            conn.execute(upsert, r)
        conn.commit()
        after = conn.execute("SELECT COUNT(*) FROM sca_leads").fetchone()[0]
    finally:
        conn.close()

    finished = dt.datetime.now().astimezone().replace(microsecond=0)
    print(f"  leads in table: {after}  (+{after - before} new)")

    runs = load_json(score_runs_json_for(module), {"schema_version": 1, "runs": []})
    runs["runs"].append({
        "run_id": run_id,
        "started_at": started.isoformat(),
        "finished_at": finished.isoformat(),
        "scored": len(rows),
        "bands": dict(bands),
        "categories": dict(cats),
        "rebuild": args.rebuild,
        "leads_count_after": after,
    })
    atomic_write_json(score_runs_json_for(module), runs)
    print(f"  ledger:         {score_runs_json_for(module).relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    p = argparse.ArgumentParser(
        description="Step 3 (score): enriched permits -> banded sca_leads.")
    p.add_argument("--module", default="Permit", choices=sorted(MODULES))
    p.add_argument("--rebuild", action="store_true",
                   help="DELETE all sca_leads before scoring (clean rebuild).")
    p.add_argument("--since", help="Only records with apply_date >= this (ISO date).")
    p.add_argument("--start-year", type=int, help="Only apply_date year >= this.")
    p.add_argument("--end-year", type=int, help="Only apply_date year <= this.")
    p.add_argument("--limit", type=int, help="Score only the first N records.")
    p.add_argument("--dry-run", action="store_true",
                   help="Report band/category distribution without writing.")
    raise SystemExit(main(p.parse_args()))

"""Step 1: parse cached search JSON into sca_permits.

Reads every cached page (outputs/raw/sca/<module>/page_*.json), maps
Result.EntityResults[] to rows, and upserts into sca_permits keyed on the record
GUID (case_id). Dedup is free: re-running re-parses the cache and updates rows.

    python3 src/step1_parse_search_results.py --module Permit
    python3 src/step1_parse_search_results.py --module Permit --dry-run

Outputs:
    sca_permits rows in outputs/sca_permits.db
    outputs/step_1/runs_<module>.json   (audit log)
"""

from __future__ import annotations

import argparse
import datetime as dt
import re
import sys
from pathlib import Path

# Allow `python src/step1_*.py` from anywhere
sys.path.insert(0, str(Path(__file__).resolve().parent))

from utils.config import MODULES
from utils.io import ROOT, atomic_write_json, connect, load_json
from utils.step_1.parsing import COLUMNS, parse_page

OUTPUTS_DIR = ROOT / "outputs" / "step_1"
_PAGE_NUM = re.compile(r"page_(\d+)\.json$")


def raw_dir_for(module: str) -> Path:
    return ROOT / "outputs" / "raw" / "sca" / MODULES[module]["raw_subdir"]


def runs_json_for(module: str) -> Path:
    return OUTPUTS_DIR / f"runs_{MODULES[module]['raw_subdir']}.json"


def _upsert_sql() -> str:
    cols = COLUMNS + ["first_seen_at", "last_seen_at"]
    placeholders = ", ".join(f":{c}" for c in cols)
    # On conflict, refresh every column EXCEPT the PK and first_seen_at.
    updates = ", ".join(
        f"{c}=excluded.{c}" for c in cols if c not in ("case_id", "first_seen_at"))
    return (f"INSERT INTO sca_permits ({', '.join(cols)}) "
            f"VALUES ({placeholders}) "
            f"ON CONFLICT(case_id) DO UPDATE SET {updates}")


def main(module: str, dry_run: bool) -> int:
    if module not in MODULES:
        print(f"[error] unknown module {module!r}; known: {sorted(MODULES)}",
              file=sys.stderr)
        return 2

    started = dt.datetime.now().astimezone().replace(microsecond=0)
    run_id = started.strftime("%Y-%m-%d_%H%M%S")
    raw_dir = raw_dir_for(module)
    pages = sorted(raw_dir.glob("page_*.json"))
    now_iso = started.isoformat()

    print(f"[{run_id}] module:   {module}")
    print(f"[{run_id}] cache:    {raw_dir.relative_to(ROOT)} ({len(pages)} pages)")
    if not pages:
        print("  no cached pages — run step 0 first.")
        return 1

    # Parse all pages into rows.
    all_rows: list[dict] = []
    for page_file in pages:
        m = _PAGE_NUM.search(page_file.name)
        page_num = int(m.group(1)) if m else 0
        result = load_json(page_file, {})
        all_rows.extend(parse_page(result, module, page_num))

    distinct_ids = {r["case_id"] for r in all_rows}
    print(f"  parsed rows:        {len(all_rows)}")
    print(f"  distinct case_ids:  {len(distinct_ids)}")

    if dry_run:
        print("  DRY RUN — not writing to DB.")
        sample = all_rows[:3]
        for r in sample:
            print(f"    {r['case_number']:<16} {r['case_type'] or '':<26} "
                  f"{r['case_status'] or '':<18} {r['address_norm'] or ''}")
        return 0

    conn = connect()
    sql = _upsert_sql()
    try:
        before = conn.execute("SELECT COUNT(*) FROM sca_permits").fetchone()[0]
        for r in all_rows:
            params = dict(r)
            params["first_seen_at"] = now_iso
            params["last_seen_at"] = now_iso
            conn.execute(sql, params)
        conn.commit()
        after = conn.execute("SELECT COUNT(*) FROM sca_permits").fetchone()[0]
    finally:
        conn.close()

    finished = dt.datetime.now().astimezone().replace(microsecond=0)
    new_rows = after - before
    print(f"  rows in table:      {after}  (+{new_rows} new this run)")
    print(f"  upserts:            {len(all_rows)} "
          f"(new case_ids: {new_rows}, updated existing: {len(all_rows) - new_rows})")

    runs = load_json(runs_json_for(module), {"schema_version": 1, "runs": []})
    runs["runs"].append({
        "run_id": run_id,
        "started_at": started.isoformat(),
        "finished_at": finished.isoformat(),
        "pages_parsed": len(pages),
        "rows_parsed": len(all_rows),
        "distinct_case_ids": len(distinct_ids),
        "table_count_after": after,
        "new_case_ids": new_rows,
    })
    atomic_write_json(runs_json_for(module), runs)
    print(f"  ledger:             {runs_json_for(module).relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    p = argparse.ArgumentParser(
        description="Step 1: parse cached EnerGov search JSON into sca_permits.")
    p.add_argument("--module", default="Permit", choices=sorted(MODULES),
                   help="Which module to parse (default: Permit).")
    p.add_argument("--dry-run", action="store_true",
                   help="Parse and report counts without writing to the DB.")
    args = p.parse_args()
    raise SystemExit(main(module=args.module, dry_run=args.dry_run))

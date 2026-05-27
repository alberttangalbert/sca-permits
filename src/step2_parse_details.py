"""Step 2 (parse): load cached detail JSON into sca_permit_detail + contacts.

Reads every cached detail file (outputs/raw/sca/<module>_detail/<case_id>.json),
upserts the 1:1 detail row, and replaces that record's contact rows (DELETE +
re-INSERT, so re-parsing is idempotent and free).

    python3 src/step2_parse_details.py
    python3 src/step2_parse_details.py --dry-run

Outputs:
    sca_permit_detail + sca_permit_contacts rows in outputs/sca_permits.db
    outputs/step_2/parse_runs_<module>.json   (audit log)
"""

from __future__ import annotations

import argparse
import datetime as dt
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from utils.config import MODULES
from utils.io import ROOT, atomic_write_json, connect, load_json
from utils.step_2.parsing import (CONTACT_COLUMNS, DETAIL_COLUMNS,
                                   parse_contacts, parse_detail)

OUTPUTS_DIR = ROOT / "outputs" / "step_2"


def raw_dir_for(module: str) -> Path:
    return ROOT / "outputs" / "raw" / "sca" / f"{MODULES[module]['raw_subdir']}_detail"


def parse_runs_json_for(module: str) -> Path:
    return OUTPUTS_DIR / f"parse_runs_{MODULES[module]['raw_subdir']}.json"


def _detail_upsert_sql() -> str:
    cols = DETAIL_COLUMNS + ["detail_parsed_at"]
    placeholders = ", ".join(f":{c}" for c in cols)
    updates = ", ".join(f"{c}=excluded.{c}" for c in cols if c != "case_id")
    return (f"INSERT INTO sca_permit_detail ({', '.join(cols)}) "
            f"VALUES ({placeholders}) "
            f"ON CONFLICT(case_id) DO UPDATE SET {updates}")


def _contact_insert_sql() -> str:
    cols = CONTACT_COLUMNS + ["parsed_at"]
    placeholders = ", ".join(f":{c}" for c in cols)
    return (f"INSERT INTO sca_permit_contacts ({', '.join(cols)}) "
            f"VALUES ({placeholders})")


def main(module: str, dry_run: bool, limit: int | None) -> int:
    if module not in MODULES:
        print(f"[error] unknown module {module!r}", file=sys.stderr)
        return 2

    started = dt.datetime.now().astimezone().replace(microsecond=0)
    run_id = started.strftime("%Y-%m-%d_%H%M%S")
    now_iso = started.isoformat()
    raw_dir = raw_dir_for(module)
    files = sorted(raw_dir.glob("*.json"))
    if limit:
        files = files[:limit]

    print(f"[{run_id}] module:   {module}")
    print(f"[{run_id}] cache:    {raw_dir.relative_to(ROOT)} ({len(files)} files)")
    if not files:
        print("  no cached detail — run step2_fetch_details.py first.")
        return 1

    detail_rows, contact_batches = [], []
    total_contacts = 0
    for f in files:
        case_id = f.stem
        result = load_json(f, {})
        detail_rows.append(parse_detail(result, case_id))
        contacts = parse_contacts(result, case_id)
        contact_batches.append((case_id, contacts))
        total_contacts += len(contacts)

    print(f"  detail rows:   {len(detail_rows)}")
    print(f"  contact rows:  {total_contacts}")

    if dry_run:
        print("  DRY RUN — not writing to DB.")
        for d in detail_rows[:3]:
            print(f"    {d['case_id']}  val={d['valuation']} "
                  f"contacts={d['contact_count']} holds={d['hold_count']} "
                  f"parcel={d['main_parcel']}")
        return 0

    conn = connect()
    detail_sql, contact_sql = _detail_upsert_sql(), _contact_insert_sql()
    try:
        before_d = conn.execute("SELECT COUNT(*) FROM sca_permit_detail").fetchone()[0]
        for d in detail_rows:
            d["detail_parsed_at"] = now_iso
            conn.execute(detail_sql, d)
        for case_id, contacts in contact_batches:
            conn.execute("DELETE FROM sca_permit_contacts WHERE case_id = ?",
                         (case_id,))
            for c in contacts:
                c["parsed_at"] = now_iso
                conn.execute(contact_sql, c)
        conn.commit()
        after_d = conn.execute("SELECT COUNT(*) FROM sca_permit_detail").fetchone()[0]
        after_c = conn.execute("SELECT COUNT(*) FROM sca_permit_contacts").fetchone()[0]
    finally:
        conn.close()

    finished = dt.datetime.now().astimezone().replace(microsecond=0)
    print(f"  detail in table:   {after_d}  (+{after_d - before_d} new)")
    print(f"  contacts in table: {after_c}")

    runs = load_json(parse_runs_json_for(module), {"schema_version": 1, "runs": []})
    runs["runs"].append({
        "run_id": run_id,
        "started_at": started.isoformat(),
        "finished_at": finished.isoformat(),
        "files_parsed": len(files),
        "detail_rows": len(detail_rows),
        "contact_rows": total_contacts,
        "detail_count_after": after_d,
        "contacts_count_after": after_c,
    })
    atomic_write_json(parse_runs_json_for(module), runs)
    print(f"  ledger:            {parse_runs_json_for(module).relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    p = argparse.ArgumentParser(
        description="Step 2 (parse): cached detail JSON -> detail + contacts.")
    p.add_argument("--module", default="Permit", choices=sorted(MODULES))
    p.add_argument("--dry-run", action="store_true",
                   help="Parse and report counts without writing to the DB.")
    p.add_argument("--limit", type=int, help="Parse only the first N cached files.")
    args = p.parse_args()
    raise SystemExit(main(module=args.module, dry_run=args.dry_run, limit=args.limit))

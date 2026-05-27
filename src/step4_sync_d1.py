"""Step 4 (sync): publish scored leads to Cloudflare D1.

Builds one denormalized `sca_leads` table (the lead + the permit/address context
a frontend needs) and emits it as portable SQL. Two modes:

    # DEFAULT — generate SQL locally (no network, no credentials):
    python3 src/step4_sync_d1.py
        -> outputs/step_4/d1_schema.sql   (CREATE TABLE)
        -> outputs/step_4/d1_sync.sql     (idempotent upserts)
      then run it yourself with wrangler (uses YOUR auth), e.g.:
        wrangler d1 execute <DB> --remote --file=outputs/step_4/d1_schema.sql
        wrangler d1 execute <DB> --remote --file=outputs/step_4/d1_sync.sql

    # --execute — POST to the D1 HTTP API using YOUR env credentials:
    CF_ACCOUNT_ID=… CF_D1_DATABASE_ID=… CF_API_TOKEN=… \
        python3 src/step4_sync_d1.py --execute

By default only actionable leads (HIGH/MEDIUM/LOW) are pushed; DROP noise stays
local. Use --all to include everything, --band to pick bands.

This pushes data the funnel OWNS to the user's OWN store — but the leads contain
homeowner contact PII, so the remote push (--execute) is opt-in and never runs
without the user's credentials in the environment.
"""

from __future__ import annotations

import argparse
import datetime as dt
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from utils.io import ROOT, connect

OUTPUTS_DIR = ROOT / "outputs" / "step_4"
SCHEMA_PATH = OUTPUTS_DIR / "d1_schema.sql"
SYNC_PATH = OUTPUTS_DIR / "d1_sync.sql"
TABLE = "sca_leads"
CHUNK = 100  # rows per multi-row INSERT (and per HTTP request batch)

# (column, SQL type) for the D1 export table — a denormalized lead + context view.
EXPORT_COLUMNS = [
    ("case_id", "TEXT PRIMARY KEY"), ("case_number", "TEXT"),
    ("lead_score", "REAL"), ("lead_band", "TEXT"), ("category", "TEXT"),
    ("status_bucket", "TEXT"), ("case_status", "TEXT"),
    ("valuation", "REAL"), ("additional_sqft", "REAL"), ("num_stories", "REAL"),
    ("construction_type", "TEXT"), ("blocking_hold", "INTEGER"),
    ("has_contractor", "INTEGER"),
    ("address_display", "TEXT"), ("main_parcel", "TEXT"),
    ("apply_date", "TEXT"), ("issue_date", "TEXT"), ("description", "TEXT"),
    ("owner_name", "TEXT"), ("owner_email", "TEXT"), ("owner_phone", "TEXT"),
    ("contractor_name", "TEXT"), ("scored_at", "TEXT"),
    ("cluster_id", "TEXT"), ("cluster_key_type", "TEXT"),  # group permits into projects
]
COLS = [c for c, _ in EXPORT_COLUMNS]

# Column order matches EXPORT_COLUMNS / COLS exactly, so rows map 1:1.
SELECT_SQL = f"""
SELECT l.case_id, p.case_number, l.lead_score, l.lead_band, l.category,
       l.status_bucket, p.case_status, l.valuation, l.additional_sqft,
       l.num_stories, l.construction_type, l.blocking_hold, l.has_contractor,
       p.address_display, p.main_parcel, p.apply_date, p.issue_date,
       p.description, l.owner_name, l.owner_email, l.owner_phone,
       l.contractor_name, l.scored_at, l.cluster_id, l.cluster_key_type
FROM sca_leads l JOIN sca_permits p USING(case_id)
"""


def _schema_sql() -> str:
    cols = ",\n  ".join(f"{c} {t}" for c, t in EXPORT_COLUMNS)
    return (f"CREATE TABLE IF NOT EXISTS {TABLE} (\n  {cols}\n);\n"
            f"CREATE INDEX IF NOT EXISTS idx_{TABLE}_band ON {TABLE}(lead_band);\n"
            f"CREATE INDEX IF NOT EXISTS idx_{TABLE}_score ON {TABLE}(lead_score);\n")


def _lit(v) -> str:
    """SQL literal for a generated statement (None->NULL, escape quotes)."""
    if v is None:
        return "NULL"
    if isinstance(v, bool):
        return "1" if v else "0"
    if isinstance(v, (int, float)):
        return repr(v)
    return "'" + str(v).replace("'", "''") + "'"


def _fetch_rows(conn, bands, limit):
    where, params = [], []
    if bands:
        where.append(f"l.lead_band IN ({', '.join('?' for _ in bands)})")
        params += bands
    sql = SELECT_SQL + (f"WHERE {' AND '.join(where)} " if where else "")
    sql += "ORDER BY l.lead_score DESC"
    if limit:
        sql += " LIMIT ?"; params.append(limit)
    return conn.execute(sql, params).fetchall()


def _insert_statements(rows) -> list[str]:
    cols_sql = ", ".join(COLS)
    updates = ", ".join(f"{c}=excluded.{c}" for c in COLS if c != "case_id")
    stmts = []
    for i in range(0, len(rows), CHUNK):
        values = ",\n  ".join("(" + ", ".join(_lit(v) for v in r) + ")"
                              for r in rows[i:i + CHUNK])
        stmts.append(f"INSERT INTO {TABLE} ({cols_sql}) VALUES\n  {values}\n"
                     f"ON CONFLICT(case_id) DO UPDATE SET {updates};")
    return stmts


def _execute_remote(statements, log=print) -> int:
    """POST each statement batch to the D1 HTTP API using env credentials."""
    import requests
    acct = os.environ.get("CF_ACCOUNT_ID")
    dbid = os.environ.get("CF_D1_DATABASE_ID")
    token = os.environ.get("CF_API_TOKEN")
    if not all([acct, dbid, token]):
        log("[error] --execute needs CF_ACCOUNT_ID, CF_D1_DATABASE_ID, and "
            "CF_API_TOKEN in the environment. Aborting (no remote call made).")
        return 2
    url = (f"https://api.cloudflare.com/client/v4/accounts/{acct}"
           f"/d1/database/{dbid}/query")
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    sess = requests.Session()
    for n, sql in enumerate(statements, 1):
        resp = sess.post(url, headers=headers, json={"sql": sql}, timeout=60)
        if resp.status_code != 200 or not resp.json().get("success", False):
            log(f"[error] batch {n}/{len(statements)} failed: HTTP "
                f"{resp.status_code} {resp.text[:200]!r}")
            return 1
        log(f"  pushed batch {n}/{len(statements)}")
    return 0


def main(args) -> int:
    OUTPUTS_DIR.mkdir(parents=True, exist_ok=True)
    bands = None if args.all else (
        [b.strip().upper() for b in args.band.split(",")] if args.band
        else ["HIGH", "MEDIUM", "LOW"])

    conn = connect()
    try:
        rows = _fetch_rows(conn, bands, args.limit)
    finally:
        conn.close()

    schema, inserts = _schema_sql(), _insert_statements(rows)
    print(f"[step4] export rows: {len(rows)}  "
          f"(bands={bands or 'ALL'})  batches={len(inserts)}")

    SCHEMA_PATH.write_text(schema)
    SYNC_PATH.write_text("\n".join(inserts) + ("\n" if inserts else ""))
    print(f"  wrote {SCHEMA_PATH.relative_to(ROOT)}")
    print(f"  wrote {SYNC_PATH.relative_to(ROOT)}")

    if not args.execute:
        print("\n  Generated SQL only (no remote call). To publish, either run "
              "with --execute\n  (needs CF_* env vars) or apply the files with "
              "wrangler using your own auth:")
        print(f"    wrangler d1 execute <DB> --remote --file={SCHEMA_PATH.relative_to(ROOT)}")
        print(f"    wrangler d1 execute <DB> --remote --file={SYNC_PATH.relative_to(ROOT)}")
        return 0

    print("\n  --execute: pushing to D1 HTTP API …")
    rc = _execute_remote([schema, *inserts])
    if rc == 0:
        print(f"  done — {len(rows)} leads synced to D1.")
    return rc


if __name__ == "__main__":
    p = argparse.ArgumentParser(
        description="Step 4 (sync): scored leads -> Cloudflare D1 (SQL gen or push).")
    p.add_argument("--all", action="store_true",
                   help="Include DROP-band rows too (default: HIGH/MEDIUM/LOW only).")
    p.add_argument("--band", help="Comma-separated bands to export (e.g. 'HIGH,MEDIUM').")
    p.add_argument("--limit", type=int, help="Cap exported rows.")
    p.add_argument("--execute", action="store_true",
                   help="POST to the D1 HTTP API using CF_* env vars (opt-in).")
    raise SystemExit(main(p.parse_args()))

"""Step 4 (sync): publish scored leads / project clusters to Cloudflare D1.

Two export targets, both into the shared `permits` D1 (the `sca_` table prefix
keeps them clear of the other cities' tables):

    sca_leads          one row per scored permit (lead + address/contact/cluster)
    sca_lead_clusters  one row per project (--clusters) — the deduped call list

Emitted as portable SQL. Modes:

    # DEFAULT — generate SQL locally (no network, no credentials):
    python3 src/step4_sync_d1.py                 # leads     -> d1_schema.sql / d1_sync.sql
    python3 src/step4_sync_d1.py --clusters       # projects  -> d1_clusters_{schema,sync}.sql
      then apply with wrangler using YOUR auth, e.g.:
        wrangler d1 execute <DB> --remote --file=outputs/step_4/d1_schema.sql

    # --execute — POST to the D1 HTTP API using YOUR env credentials:
    CF_ACCOUNT_ID=… CF_D1_DATABASE_ID=… CF_API_TOKEN=… \
        python3 src/step4_sync_d1.py --clusters --execute

By default only actionable bands (HIGH/MEDIUM/LOW) are pushed; DROP noise stays
local. Use --all to include everything, --band to pick bands.

Pre-2020 permits are excluded by default (--since 2020-01-01). The recency_factor
0.1 floor isn't quite enough on its own — a handful of pre-2020 "Approved"
zombies (work long done) still squeak past band LOW (score 7-9). This matches
the tick's --backfill-since 2020-01-01 floor so the export and the enrichment
agree on the same horizon. Pass --since "" to publish the full archival history.

The upsert never deletes, so a lead that LEAVES the actionable set (e.g. a permit
gets issued/completed -> its score drops to band DROP) would linger forever in
D1 as a stale "actionable" row. Pass --prune to append a mirror-delete that makes
D1 contain exactly this export (scoped to the sca_ table; can't touch other
cities). --prune is incompatible with --limit (a partial export must not prune).

This pushes data the funnel OWNS to the user's OWN store — but rows contain
homeowner contact PII, so the remote push (--execute) is opt-in and never runs
without the user's credentials in the environment.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from utils.io import ROOT, connect

OUTPUTS_DIR = ROOT / "outputs" / "step_4"
CHUNK = 100  # rows per multi-row INSERT (and per HTTP request batch)

# Each export target: the D1 table, its columns (first column is the PK / upsert
# conflict target), the SELECT that fills it IN THE SAME COLUMN ORDER, the band
# column used for --band filtering, sort, and the indexes to create.
LEADS_SPEC = {
    "name": "leads",
    "table": "sca_leads",
    "schema_file": "d1_schema.sql",
    "sync_file": "d1_sync.sql",
    "columns": [
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
        ("cluster_id", "TEXT"), ("cluster_key_type", "TEXT"),
    ],
    "select": """
SELECT l.case_id, p.case_number, l.lead_score, l.lead_band, l.category,
       l.status_bucket, p.case_status, l.valuation, l.additional_sqft,
       l.num_stories, l.construction_type, l.blocking_hold, l.has_contractor,
       p.address_display, p.main_parcel, p.apply_date, p.issue_date,
       p.description, l.owner_name, l.owner_email, l.owner_phone,
       l.contractor_name, l.scored_at, l.cluster_id, l.cluster_key_type
FROM sca_leads l JOIN sca_permits p USING(case_id)
""",
    "band_col": "l.lead_band",
    "date_col": "p.apply_date",
    "order_by": "l.lead_score DESC",
    "indexes": [("band", "lead_band"), ("score", "lead_score")],
}

CLUSTERS_SPEC = {
    "name": "clusters",
    "table": "sca_lead_clusters",
    "schema_file": "d1_clusters_schema.sql",
    "sync_file": "d1_clusters_sync.sql",
    "columns": [
        ("cluster_id", "TEXT PRIMARY KEY"), ("key_type", "TEXT"),
        ("permit_count", "INTEGER"), ("max_lead_score", "REAL"),
        ("top_band", "TEXT"), ("categories", "TEXT"),
        ("total_valuation", "REAL"), ("max_valuation", "REAL"),
        ("primary_case_id", "TEXT"), ("address_display", "TEXT"),
        ("main_parcel", "TEXT"), ("owner_name", "TEXT"),
        ("owner_email", "TEXT"), ("owner_phone", "TEXT"),
        ("has_contractor", "INTEGER"), ("first_apply_date", "TEXT"),
        ("last_apply_date", "TEXT"),
    ],
    "select": """
SELECT cluster_id, key_type, permit_count, max_lead_score, top_band, categories,
       total_valuation, max_valuation, primary_case_id, address_display,
       main_parcel, owner_name, owner_email, owner_phone, has_contractor,
       first_apply_date, last_apply_date
FROM sca_lead_clusters
""",
    "band_col": "top_band",
    # Cluster is kept if its MOST RECENT permit is post-cutoff — a cluster with
    # any live activity in-window is in-scope even if older permits drag back.
    "date_col": "last_apply_date",
    "order_by": "max_lead_score DESC",
    "indexes": [("band", "top_band"), ("score", "max_lead_score")],
}


def _schema_sql(spec) -> str:
    cols = ",\n  ".join(f"{c} {t}" for c, t in spec["columns"])
    idx = "".join(
        f"CREATE INDEX IF NOT EXISTS idx_{spec['table']}_{name} "
        f"ON {spec['table']}({expr});\n" for name, expr in spec["indexes"])
    return f"CREATE TABLE IF NOT EXISTS {spec['table']} (\n  {cols}\n);\n" + idx


def _lit(v) -> str:
    """SQL literal for a generated statement (None->NULL, escape quotes)."""
    if v is None:
        return "NULL"
    if isinstance(v, bool):
        return "1" if v else "0"
    if isinstance(v, (int, float)):
        return repr(v)
    return "'" + str(v).replace("'", "''") + "'"


def _fetch_rows(conn, spec, bands, since, limit):
    where, params = [], []
    if bands:
        where.append(f"{spec['band_col']} IN ({', '.join('?' for _ in bands)})")
        params += bands
    if since:
        # Treat NULL/empty apply_date as IN-scope: a known-current permit that
        # happens to be missing apply_date should NOT be silently filtered out.
        # Pre-2020 zombies all HAVE apply_date, so this floor still excludes them.
        where.append(f"({spec['date_col']} IS NULL OR {spec['date_col']} >= ?)")
        params.append(since)
    sql = spec["select"] + (f"WHERE {' AND '.join(where)} " if where else "")
    sql += "ORDER BY " + spec["order_by"]
    if limit:
        sql += " LIMIT ?"; params.append(limit)
    return conn.execute(sql, params).fetchall()


def _insert_statements(spec, rows) -> list[str]:
    cols = [c for c, _ in spec["columns"]]
    pk = cols[0]
    cols_sql = ", ".join(cols)
    updates = ", ".join(f"{c}=excluded.{c}" for c in cols if c != pk)
    stmts = []
    for i in range(0, len(rows), CHUNK):
        values = ",\n  ".join("(" + ", ".join(_lit(v) for v in r) + ")"
                              for r in rows[i:i + CHUNK])
        stmts.append(f"INSERT INTO {spec['table']} ({cols_sql}) VALUES\n  {values}\n"
                     f"ON CONFLICT({pk}) DO UPDATE SET {updates};")
    return stmts


def _prune_statement(spec, rows) -> str:
    """Mirror semantics: delete remote rows NOT in this export, so a lead that
    left the actionable set (e.g. issued/completed -> band DROP) doesn't linger
    in D1 forever (the upsert alone never removes it). Runs AFTER the inserts, so
    the fresh rows are already present even though delete is the last statement.
    Scoped to spec['table'] (the sca_ prefix) -> it can't touch other cities'
    tables in the shared D1. An empty export mirrors to an empty table."""
    pk = spec["columns"][0][0]
    if not rows:
        return f"DELETE FROM {spec['table']};"
    keep = ", ".join(_lit(r[0]) for r in rows)
    return f"DELETE FROM {spec['table']} WHERE {pk} NOT IN ({keep});"


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
    spec = CLUSTERS_SPEC if args.clusters else LEADS_SPEC
    bands = None if args.all else (
        [b.strip().upper() for b in args.band.split(",")] if args.band
        else ["HIGH", "MEDIUM", "LOW"])

    if args.prune and args.limit:
        print("[error] --prune mirrors the full export to D1, so it can't be "
              "combined with --limit (a partial export would delete the rest). "
              "Aborting (no files written).")
        return 2

    conn = connect()
    try:
        rows = _fetch_rows(conn, spec, bands, args.since or None, args.limit)
    finally:
        conn.close()

    schema, inserts = _schema_sql(spec), _insert_statements(spec, rows)
    prune = _prune_statement(spec, rows) if args.prune else None
    body = inserts + ([prune] if prune else [])
    schema_path = OUTPUTS_DIR / spec["schema_file"]
    sync_path = OUTPUTS_DIR / spec["sync_file"]
    print(f"[step4] {spec['name']}: export rows {len(rows)}  "
          f"(bands={bands or 'ALL'}, since={args.since or 'ALL'})  "
          f"batches={len(inserts)}{'  +prune' if prune else ''}")

    schema_path.write_text(schema)
    sync_path.write_text(("\n".join(body) + "\n") if body else "")
    print(f"  wrote {schema_path.relative_to(ROOT)}")
    print(f"  wrote {sync_path.relative_to(ROOT)}"
          + ("  (ends with a prune DELETE -> D1 mirrors this export)" if prune else ""))

    if not args.execute:
        print("\n  Generated SQL only (no remote call). To publish, either run "
              "with --execute\n  (needs CF_* env vars) or apply the files with "
              "wrangler using your own auth:")
        print(f"    wrangler d1 execute <DB> --remote --file={schema_path.relative_to(ROOT)}")
        print(f"    wrangler d1 execute <DB> --remote --file={sync_path.relative_to(ROOT)}")
        return 0

    print("\n  --execute: pushing to D1 HTTP API …")
    rc = _execute_remote([schema, *body])
    if rc == 0:
        print(f"  done — {len(rows)} {spec['name']} rows synced to D1"
              + (" (stale rows pruned)." if prune else "."))
    return rc


if __name__ == "__main__":
    p = argparse.ArgumentParser(
        description="Step 4 (sync): leads / clusters -> Cloudflare D1 (SQL gen or push).")
    p.add_argument("--clusters", action="store_true",
                   help="Export sca_lead_clusters (deduped projects) instead of per-permit leads.")
    p.add_argument("--all", action="store_true",
                   help="Include DROP-band rows too (default: HIGH/MEDIUM/LOW only).")
    p.add_argument("--band", help="Comma-separated bands to export (e.g. 'HIGH,MEDIUM').")
    p.add_argument("--since", default="2020-01-01",
                   help="Floor on apply_date (leads) / last_apply_date (clusters). "
                        "Default 2020-01-01 matches the tick backfill horizon and "
                        "kills the 9 pre-2020 'Approved' zombie LOW leads. "
                        "Pass --since '' to publish the full archival history.")
    p.add_argument("--limit", type=int, help="Cap exported rows.")
    p.add_argument("--prune", action="store_true",
                   help="Append a DELETE so D1 mirrors exactly this export "
                        "(removes leads that left the actionable set). Cannot be "
                        "combined with --limit.")
    p.add_argument("--execute", action="store_true",
                   help="POST to the D1 HTTP API using CF_* env vars (opt-in).")
    raise SystemExit(main(p.parse_args()))

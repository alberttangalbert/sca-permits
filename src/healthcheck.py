"""Healthcheck — read-only integrity + consistency assertions over the DB.

Run it standalone or as the tick's final step to catch data drift (orphans,
invalid scores, cluster/lead mismatches, unapplied migrations) before it reaches
D1 / the frontend. Read-only: opens the DB, asserts, prints PASS/WARN/FAIL.

    python3 src/healthcheck.py          # exit 0 unless a FAIL check trips
    python3 src/healthcheck.py -q       # only print WARN/FAIL lines

Exit code: 1 if any FAIL, else 0 (WARN never fails the run).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from utils.io import DB_PATH, MIGRATIONS_DIR, ROOT, connect

# Count-based checks: each SQL returns the number of OFFENDING rows; 0 == healthy.
# severity FAIL trips a nonzero exit; WARN is advisory.
COUNT_CHECKS = [
    ("FAIL", "permits: case_id non-null/non-empty",
     "SELECT COUNT(*) FROM sca_permits WHERE case_id IS NULL OR case_id=''"),
    ("FAIL", "permits: no duplicate case_id",
     "SELECT COUNT(*) FROM (SELECT case_id FROM sca_permits "
     "GROUP BY case_id HAVING COUNT(*)>1)"),
    ("FAIL", "detail: no orphan rows",
     "SELECT COUNT(*) FROM sca_permit_detail d "
     "LEFT JOIN sca_permits p USING(case_id) WHERE p.case_id IS NULL"),
    ("FAIL", "contacts: no orphan rows",
     "SELECT COUNT(*) FROM sca_permit_contacts c "
     "LEFT JOIN sca_permits p USING(case_id) WHERE p.case_id IS NULL"),
    ("FAIL", "leads: no orphan rows",
     "SELECT COUNT(*) FROM sca_leads l "
     "LEFT JOIN sca_permits p USING(case_id) WHERE p.case_id IS NULL"),
    ("FAIL", "leads: every lead has a detail row",
     "SELECT COUNT(*) FROM sca_leads l "
     "LEFT JOIN sca_permit_detail d USING(case_id) WHERE d.case_id IS NULL"),
    ("FAIL", "leads: lead_score within [0,100]",
     "SELECT COUNT(*) FROM sca_leads "
     "WHERE lead_score IS NULL OR lead_score < 0 OR lead_score > 100"),
    ("FAIL", "leads: lead_band in valid set",
     "SELECT COUNT(*) FROM sca_leads "
     "WHERE lead_band NOT IN ('HIGH','MEDIUM','LOW','DROP')"),
    ("FAIL", "clusters: no empty clusters",
     "SELECT COUNT(*) FROM sca_lead_clusters c WHERE NOT EXISTS "
     "(SELECT 1 FROM sca_leads l WHERE l.cluster_id=c.cluster_id)"),
    ("FAIL", "clusters: primary_case_id is a member",
     "SELECT COUNT(*) FROM sca_lead_clusters c WHERE c.primary_case_id IS NOT NULL "
     "AND NOT EXISTS (SELECT 1 FROM sca_leads l "
     "WHERE l.cluster_id=c.cluster_id AND l.case_id=c.primary_case_id)"),
    ("WARN", "leads: every scored lead is clustered",
     "SELECT COUNT(*) FROM sca_leads WHERE cluster_id IS NULL"),
    ("WARN", "permits: apply_date not in the future",
     "SELECT COUNT(*) FROM sca_permits "
     "WHERE apply_date > strftime('%Y-%m-%dT%H:%M:%S','now','+2 days')"),
    # Catches a SILENT step-2 regression: recent permits arrive via search but
    # never get a detail row -> they vanish from scoring with no other alarm
    # (the "every lead has detail" check passes trivially since leads derive
    # from detail). The tick re-fetches the whole 2y window each run, so a permit
    # filed in the last 90d has had many fetch attempts; if it still lacks detail
    # that's a fetch outage or a permanently-erroring record. WARN, not FAIL, so a
    # stray transient miss doesn't break the run.
    ("WARN", "detail: recent permits (90d) have detail coverage",
     "SELECT COUNT(*) FROM sca_permits p WHERE p.apply_date >= "
     "date('now','-90 days') AND NOT EXISTS "
     "(SELECT 1 FROM sca_permit_detail d WHERE d.case_id=p.case_id)"),
    # Actionable leads (HIGH/MEDIUM) the GC can't actually reach — no email AND
    # no phone on the chosen owner contact. The pipeline scored them correctly;
    # the gap is in the source's contact data, so WARN (never FAIL) and let the
    # count trend: it tells the operator how much of the callable funnel is dead.
    ("WARN", "leads: actionable (HIGH/MEDIUM) leads have a reachable contact",
     "SELECT COUNT(*) FROM sca_leads WHERE lead_band IN ('HIGH','MEDIUM') "
     "AND (owner_email IS NULL OR owner_email='') "
     "AND (owner_phone IS NULL OR owner_phone='')"),
]


def _consistency_checks(conn) -> list[tuple]:
    """Cross-table invariants that need more than a single count. Returns
    (severity, label, ok, detail) tuples."""
    out = []

    lead_n = conn.execute("SELECT COUNT(*) FROM sca_leads").fetchone()[0]
    sum_pc = conn.execute(
        "SELECT COALESCE(SUM(permit_count),0) FROM sca_lead_clusters").fetchone()[0]
    out.append(("FAIL", "clusters: SUM(permit_count) == lead count",
                sum_pc == lead_n, f"{sum_pc} vs {lead_n}"))

    cluster_n = conn.execute("SELECT COUNT(*) FROM sca_lead_clusters").fetchone()[0]
    distinct_cid = conn.execute(
        "SELECT COUNT(DISTINCT cluster_id) FROM sca_leads "
        "WHERE cluster_id IS NOT NULL").fetchone()[0]
    out.append(("FAIL", "clusters: row count == distinct lead cluster_ids",
                cluster_n == distinct_cid, f"{cluster_n} vs {distinct_cid}"))

    applied = conn.execute("SELECT COUNT(*) FROM _schema_migrations").fetchone()[0]
    on_disk = len(list(MIGRATIONS_DIR.glob("*.sql")))
    out.append(("FAIL", "migrations: all on-disk files applied",
                applied == on_disk, f"{applied} applied / {on_disk} files"))

    bands = dict(conn.execute(
        "SELECT lead_band, COUNT(*) FROM sca_leads GROUP BY lead_band").fetchall())
    actionable = bands.get("HIGH", 0) + bands.get("MEDIUM", 0)
    out.append(("WARN", "leads: some actionable (HIGH/MEDIUM) leads exist",
                actionable > 0, f"{actionable} actionable"))
    return out


def main(quiet: bool) -> int:
    if not DB_PATH.exists():
        print(f"[healthcheck] no DB at {DB_PATH.relative_to(ROOT)} — run the pipeline first.")
        return 1

    conn = connect()
    results = []  # (severity, label, ok, detail)
    try:
        for severity, label, sql in COUNT_CHECKS:
            bad = conn.execute(sql).fetchone()[0]
            results.append((severity, label, bad == 0,
                            "" if bad == 0 else f"{bad} offending"))
        results += _consistency_checks(conn)
    finally:
        conn.close()

    n_fail = n_warn = 0
    print(f"[healthcheck] {DB_PATH.relative_to(ROOT)}")
    for severity, label, ok, detail in results:
        if ok:
            status = "PASS"
        elif severity == "WARN":
            status = "WARN"; n_warn += 1
        else:
            status = "FAIL"; n_fail += 1
        if ok and quiet:
            continue
        suffix = f"  ({detail})" if detail and not ok else ""
        print(f"  {status}  {label}{suffix}")

    n_pass = len(results) - n_fail - n_warn
    print(f"[healthcheck] {n_pass} pass, {n_warn} warn, {n_fail} fail")
    return 1 if n_fail else 0


if __name__ == "__main__":
    p = argparse.ArgumentParser(description="Read-only DB integrity healthcheck.")
    p.add_argument("-q", "--quiet", action="store_true",
                   help="Only print WARN/FAIL lines (and the summary).")
    raise SystemExit(main(quiet=p.parse_args().quiet))

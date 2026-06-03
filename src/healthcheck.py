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
from utils.step_3.scoring import band as band_of_score

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
    ("FAIL", "leads: lead_score within [0,1]",
     "SELECT COUNT(*) FROM sca_leads "
     "WHERE lead_score IS NULL OR lead_score < 0 OR lead_score > 1"),
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
    # The cluster-level mirror: this is what the GC's deduped call list
    # actually looks at, and aggregate() can mine contact from older sibling
    # permits at the same parcel (e.g. BLD2024-00329's contactless 2024 TRS
    # owner gets the 2007-era LYNCH ROBERT phone from a sibling). When the
    # cluster export is fully reachable, the lead-level WARN doesn't translate
    # to a real operational gap. Tracking both side-by-side makes that visible.
    ("WARN", "clusters: actionable (HIGH/MEDIUM) clusters have a reachable contact",
     "SELECT COUNT(*) FROM sca_lead_clusters WHERE top_band IN ('HIGH','MEDIUM') "
     "AND (owner_email IS NULL OR owner_email='') "
     "AND (owner_phone IS NULL OR owner_phone='')"),
    # Invariant: any lead with surfaced owner contact info MUST have a
    # contact_role labeling its provenance — otherwise the UI can't honestly
    # render "Name (Role)" and would silently default to "Owner" on what may
    # be an architect/agent. FAIL because this is a wiring bug, not data drift.
    ("FAIL", "leads: contact_role set whenever a contact is surfaced",
     "SELECT COUNT(*) FROM sca_leads WHERE contact_role IS NULL "
     "AND ((owner_email IS NOT NULL AND owner_email != '') "
     "  OR (owner_phone IS NOT NULL AND owner_phone != '') "
     "  OR (owner_name IS NOT NULL AND owner_name != ''))"),
    # Same invariant on the cluster export — aggregate() must propagate the
    # role of the picked contact MEMBER. Without this, a refactor that
    # forgets to wire contact_role through clustering would silently strip
    # the (Architect)/(Designer) labels from the GC's project call list.
    ("FAIL", "clusters: contact_role set whenever a contact is surfaced",
     "SELECT COUNT(*) FROM sca_lead_clusters WHERE contact_role IS NULL "
     "AND ((owner_email IS NOT NULL AND owner_email != '') "
     "  OR (owner_phone IS NOT NULL AND owner_phone != '') "
     "  OR (owner_name IS NOT NULL AND owner_name != ''))"),
    # Placeholder identities must NEVER surface as a lead contact. scoring's
    # _is_placeholder drops EnerGov-conversion stamps ("EnerGov 2023Q4" /
    # energovconversion@tylertech.com, ~2,400 rows) and 'void void'/'builder
    # owner' markers before pick_contacts ranks anyone. That filter is the only
    # thing standing between the GC and a call sheet full of Tyler migration
    # noise -- but nothing RE-ASSERTS it on the output, so a refactor that broke
    # the filter (or the new property_owner_name path, which also carries an
    # owner name) would leak junk with no other alarm. FAIL: a regression here
    # silently poisons the deliverable. Keep the markers in sync with
    # scoring._PLACEHOLDER_NAMES / _ENERGOV_MIGRATION_NAME.
    ("FAIL", "leads: no placeholder identity surfaced as a contact",
     "SELECT COUNT(*) FROM sca_leads WHERE "
     "owner_email LIKE '%@tylertech.com%' "
     "OR owner_name LIKE 'EnerGov ____Q_' OR property_owner_name LIKE 'EnerGov ____Q_' "
     "OR LOWER(owner_name) IN ('void void','builder owner','test test','redacted redacted') "
     "OR LOWER(property_owner_name) IN ('void void','builder owner','test test','redacted redacted')"),
    ("FAIL", "clusters: no placeholder identity surfaced as a contact",
     "SELECT COUNT(*) FROM sca_lead_clusters WHERE "
     "owner_email LIKE '%@tylertech.com%' "
     "OR owner_name LIKE 'EnerGov ____Q_' OR property_owner_name LIKE 'EnerGov ____Q_' "
     "OR LOWER(owner_name) IN ('void void','builder owner','test test','redacted redacted') "
     "OR LOWER(property_owner_name) IN ('void void','builder owner','test test','redacted redacted')"),
    # cluster_id format must agree with cluster_key_type: PARCEL -> "P:<...>",
    # ADDRESS -> "A:<...>", SINGLETON -> "C:<...>" (the prefixes are how the
    # D1 prune step distinguishes them and how the cluster spec maps to the
    # right export. A code refactor that breaks the prefix convention would
    # silently corrupt cluster membership; catch it here.
    ("FAIL", "leads: cluster_id prefix matches cluster_key_type",
     "SELECT COUNT(*) FROM sca_leads WHERE cluster_id IS NOT NULL AND "
     "((cluster_key_type='PARCEL'    AND cluster_id NOT LIKE 'P:%') OR "
     " (cluster_key_type='ADDRESS'   AND cluster_id NOT LIKE 'A:%') OR "
     " (cluster_key_type='SINGLETON' AND cluster_id NOT LIKE 'C:%'))"),
    # Per-cluster permit_count must match actual member count. Catches drift
    # where step3b's aggregate persisted a count that doesn't match the
    # leads that now point at the cluster (e.g. partial re-cluster on a
    # subset of cases). The cluster-wide SUM invariant lower in this file
    # would catch some such drift but not a balanced miscount.
    ("FAIL", "clusters: permit_count matches actual lead members",
     """SELECT COUNT(*) FROM sca_lead_clusters c WHERE c.permit_count !=
        (SELECT COUNT(*) FROM sca_leads l WHERE l.cluster_id=c.cluster_id)"""),
    # Non-positive valuation must never round-trip into the lead row.
    # size_factor already treats <=0 as neutral, but the column should be
    # NULL'd so D1 doesn't display "-$9" or "$0" as a real valuation.
    ("FAIL", "leads: valuation is positive or NULL (never 0 or negative)",
     "SELECT COUNT(*) FROM sca_leads WHERE valuation IS NOT NULL AND valuation <= 0"),
]


def migration_set_status(applied_files: set, on_disk_files: set) -> tuple[bool, str]:
    """(ok, detail) for the migrations invariant: the set of applied filenames
    must equal the set of *.sql files on disk. Pure (no DB / FS) so the drift
    cases are unit-testable. `ok` is False when any on-disk file is unapplied OR
    any applied file is gone from disk; the detail names the offenders so a
    tripped check is actionable rather than a bare count mismatch."""
    unapplied = sorted(on_disk_files - applied_files)
    orphaned = sorted(applied_files - on_disk_files)
    detail = f"{len(applied_files)} applied / {len(on_disk_files)} on disk"
    if unapplied:
        detail += f"; UNAPPLIED: {', '.join(unapplied)}"
    if orphaned:
        detail += f"; APPLIED-BUT-MISSING: {', '.join(orphaned)}"
    return (not unapplied and not orphaned), detail


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

    # lead_band must equal band(lead_score) for EVERY row. The COUNT_CHECKS
    # verify score-in-range and band-in-set independently, but not that the two
    # AGREE -- so a partial re-score (e.g. the [0,100]->[0,1] migration leaving
    # old-threshold bands behind) or a future BANDS edit applied in code but not
    # re-scored into the table would store score=0.6/band=MEDIUM and slip
    # through. Recompute via the REAL band() so there's no threshold to drift.
    mismatched = sum(
        1 for score, b in conn.execute("SELECT lead_score, lead_band FROM sca_leads")
        if score is not None and band_of_score(score) != b)
    out.append(("FAIL", "leads: lead_band matches band(lead_score)",
                mismatched == 0, f"{mismatched} mismatched"))

    # Compare the SET of applied filenames against the SET on disk, not just the
    # counts. A count check (applied == on_disk) gives false confidence when the
    # sets drift but happen to stay equal-sized -- e.g. an applied migration's
    # file is renamed/replaced while a new one is added (1 missing + 1 extra ->
    # counts still match, schema silently wrong). The set diff also names the
    # offending files, so a tripped check is actionable ("run the pipeline to
    # apply 0005_x.sql") instead of a bare "3 applied / 4 files".
    applied_files = {row[0] for row in
                     conn.execute("SELECT filename FROM _schema_migrations")}
    on_disk_files = {f.name for f in MIGRATIONS_DIR.glob("*.sql")}
    mig_ok, mig_detail = migration_set_status(applied_files, on_disk_files)
    out.append(("FAIL", "migrations: applied set == on-disk set", mig_ok, mig_detail))

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

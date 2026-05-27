"""Step 3b (cluster): fold scored leads into one-row-per-project clusters.

Reads sca_leads (joined to sca_permits for the cluster key + address), groups by
parcel/address, writes each lead's cluster_id back, and rebuilds sca_lead_clusters
— the deduped call list (one row per project, anchored on the strongest permit).

Cheap and idempotent; re-run after any re-score:

    python3 src/step3b_cluster.py
    python3 src/step3b_cluster.py --dry-run

Outputs:
    cluster_id on sca_leads + rebuilt sca_lead_clusters in outputs/sca_permits.db
    outputs/step_3/cluster_runs_<module>.json   (audit log)
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
from utils.step_3.clustering import aggregate, cluster_key

OUTPUTS_DIR = ROOT / "outputs" / "step_3"

CLUSTER_COLUMNS = [
    "cluster_id", "key_type", "permit_count", "max_lead_score", "top_band",
    "categories", "total_valuation", "max_valuation", "primary_case_id",
    "address_display", "main_parcel", "owner_name", "owner_email", "owner_phone",
    "has_contractor", "first_apply_date", "last_apply_date",
]


def cluster_runs_json_for(module: str) -> Path:
    return OUTPUTS_DIR / f"cluster_runs_{MODULES[module]['raw_subdir']}.json"


def _insert_sql() -> str:
    cols = CLUSTER_COLUMNS + ["clustered_at"]
    placeholders = ", ".join(f":{c}" for c in cols)
    return f"INSERT INTO sca_lead_clusters ({', '.join(cols)}) VALUES ({placeholders})"


def main(args) -> int:
    started = dt.datetime.now().astimezone().replace(microsecond=0)
    run_id = started.strftime("%Y-%m-%d_%H%M%S")
    now_iso = started.isoformat()

    conn = connect()
    try:
        rows = conn.execute(
            "SELECT l.case_id, l.lead_score, l.lead_band, l.category, l.valuation, "
            "l.owner_name, l.owner_email, l.owner_phone, l.has_contractor, "
            "p.address_display, p.main_parcel, p.address_norm, p.apply_date "
            "FROM sca_leads l JOIN sca_permits p USING(case_id)").fetchall()

        clusters: dict[str, list[dict]] = defaultdict(list)
        key_for_case: list[tuple[str, str, str]] = []
        for r in rows:
            (case_id, score, band, category, valuation, o_name, o_email, o_phone,
             has_c, addr, parcel, addr_norm, apply_date) = r
            cid, ktype = cluster_key(parcel, addr_norm, case_id)
            key_for_case.append((case_id, cid, ktype))
            clusters[cid].append({
                "case_id": case_id, "lead_score": score, "lead_band": band,
                "category": category, "valuation": valuation, "owner_name": o_name,
                "owner_email": o_email, "owner_phone": o_phone, "has_contractor": has_c,
                "address_display": addr, "main_parcel": parcel, "apply_date": apply_date,
            })

        key_types = Counter(kt for _, _, kt in key_for_case)
        # Build one summary row per cluster, with that cluster's key type.
        ktype_by_cid = {cid: kt for _, cid, kt in key_for_case}
        summaries = [aggregate(cid, ktype_by_cid[cid], members)
                     for cid, members in clusters.items()]

        collapsible = len(rows) - len(clusters)
        bands = Counter(s["top_band"] for s in summaries)
        print(f"[{run_id}] leads:           {len(rows)}")
        print(f"[{run_id}] projects:        {len(clusters)}  "
              f"(collapsed {collapsible} duplicate-parcel leads)")
        print(f"[{run_id}] key types:       " +
              "  ".join(f"{k}={v}" for k, v in key_types.most_common()))
        print(f"[{run_id}] project bands:   " +
              "  ".join(f"{b}={bands.get(b,0)}" for b in ("HIGH", "MEDIUM", "LOW", "DROP")))

        if args.dry_run:
            print("  DRY RUN — not writing. Top 8 multi-permit projects:")
            multi = sorted((s for s in summaries if s["permit_count"] > 1),
                           key=lambda s: (s["max_lead_score"] or 0), reverse=True)[:8]
            for s in multi:
                print(f"    {s['max_lead_score']:5.1f} {s['top_band']:6} "
                      f"x{s['permit_count']} {str(s['address_display'])[:30]:30} "
                      f"[{s['categories']}]")
            return 0

        conn.executemany(
            "UPDATE sca_leads SET cluster_id=?, cluster_key_type=? WHERE case_id=?",
            [(cid, kt, case_id) for case_id, cid, kt in key_for_case])
        conn.execute("DELETE FROM sca_lead_clusters")
        insert = _insert_sql()
        for s in summaries:
            s["clustered_at"] = now_iso
            conn.execute(insert, s)
        conn.commit()
        after = conn.execute("SELECT COUNT(*) FROM sca_lead_clusters").fetchone()[0]
    finally:
        conn.close()

    finished = dt.datetime.now().astimezone().replace(microsecond=0)
    print(f"  clusters in table: {after}")

    runs = load_json(cluster_runs_json_for(args.module),
                     {"schema_version": 1, "runs": []})
    runs["runs"].append({
        "run_id": run_id, "started_at": started.isoformat(),
        "finished_at": finished.isoformat(),
        "leads": len(rows), "projects": len(clusters),
        "collapsed": collapsible, "key_types": dict(key_types),
        "project_bands": dict(bands),
    })
    atomic_write_json(cluster_runs_json_for(args.module), runs)
    print(f"  ledger:            {cluster_runs_json_for(args.module).relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    p = argparse.ArgumentParser(
        description="Step 3b (cluster): scored leads -> one-row-per-project clusters.")
    p.add_argument("--module", default="Permit", choices=sorted(MODULES))
    p.add_argument("--dry-run", action="store_true",
                   help="Report cluster stats without writing to the DB.")
    raise SystemExit(main(p.parse_args()))

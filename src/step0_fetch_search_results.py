"""Step 0: fetch search-result JSON from the San Carlos EnerGov CSS portal.

Pages through the public search API (anonymous; no login) and caches each page's
raw JSON for step 1 to parse. No Selenium, no postbacks, no Cloudflare gate.

    # smoke test (2 pages):
    python3 src/step0_fetch_search_results.py --module Permit --max-pages 2

    # full backfill (~530 pages @ size 100):
    python3 src/step0_fetch_search_results.py --module Permit

Outputs:
    outputs/raw/sca/<module>/page_NNNNN.json   (one cached page per file)
    outputs/step_0/runs_<module>.json          (audit log per module)
"""

from __future__ import annotations

import argparse
import datetime as dt
import sys
from pathlib import Path

# Allow `python src/step0_*.py` from anywhere
sys.path.insert(0, str(Path(__file__).resolve().parent))

from utils.config import DEFAULT_PAGE_SIZE, MODULES
from utils.io import ROOT, atomic_write_json, load_json
from utils.step_0.fetch import fetch_all

OUTPUTS_DIR = ROOT / "outputs" / "step_0"


def raw_dir_for(module: str) -> Path:
    return ROOT / "outputs" / "raw" / "sca" / MODULES[module]["raw_subdir"]


def runs_json_for(module: str) -> Path:
    return OUTPUTS_DIR / f"runs_{MODULES[module]['raw_subdir']}.json"


def main(module: str, page_size: int, max_pages: int | None,
         page_delay: float, no_cache: bool) -> int:
    if module not in MODULES:
        print(f"[error] unknown module {module!r}; known: {sorted(MODULES)}",
              file=sys.stderr)
        return 2

    meta = MODULES[module]
    started = dt.datetime.now().astimezone().replace(microsecond=0)
    run_id = started.strftime("%Y-%m-%d_%H%M%S")
    raw_dir = raw_dir_for(module)

    print(f"[{run_id}] module:    {module} (FilterModule={meta['filter_module']})")
    print(f"[{run_id}] cache dir: {raw_dir.relative_to(ROOT)}")
    print(f"[{run_id}] page size: {page_size}"
          + (f"  max pages: {max_pages}" if max_pages else "")
          + (f"  (no-cache: refetching cached pages)" if no_cache else ""))

    audit = fetch_all(
        module_label=module, filter_module=meta["filter_module"],
        sort_by=meta["sort_by"], raw_dir=raw_dir, page_size=page_size,
        max_pages=max_pages, page_delay=page_delay, no_cache=no_cache)

    finished = dt.datetime.now().astimezone().replace(microsecond=0)
    print()
    print(f"[{run_id}] complete in {audit['duration_seconds']}s")
    print(f"  total found:    {audit['total_found']}")
    print(f"  total pages:    {audit['total_pages']}")
    print(f"  pages fetched:  {audit['pages_fetched']}")
    print(f"  pages skipped:  {audit['pages_skipped']} (already cached)")
    print(f"  records seen:   {audit['records_seen']}")
    print(f"  errors:         {len(audit['errors'])}")

    runs = load_json(runs_json_for(module), {"schema_version": 1, "runs": []})
    runs["runs"].append({
        "run_id": run_id,
        "started_at": started.isoformat(),
        "finished_at": finished.isoformat(),
        **audit,
    })
    atomic_write_json(runs_json_for(module), runs)
    print(f"  ledger:         {runs_json_for(module).relative_to(ROOT)}")
    return 1 if audit["errors"] else 0


if __name__ == "__main__":
    p = argparse.ArgumentParser(
        description="Step 0: fetch San Carlos EnerGov CSS search-result JSON.")
    p.add_argument("--module", default="Permit", choices=sorted(MODULES),
                   help="Which module to scrape (default: Permit).")
    p.add_argument("--page-size", type=int, default=DEFAULT_PAGE_SIZE,
                   help=f"Records per page; allowed 10/25/50/100 "
                        f"(default {DEFAULT_PAGE_SIZE}).")
    p.add_argument("--max-pages", type=int, default=None,
                   help="Stop after N pages (smoke tests). Default: all pages.")
    p.add_argument("--page-delay", type=float, default=1.0,
                   help="Seconds between page requests (default 1.0).")
    p.add_argument("--no-cache", action="store_true",
                   help="Refetch pages even if already cached.")
    args = p.parse_args()

    raise SystemExit(main(
        module=args.module, page_size=args.page_size, max_pages=args.max_pages,
        page_delay=args.page_delay, no_cache=args.no_cache,
    ))

"""Step 0 fetch engine — date-chunked JSON search against EnerGov CSS.

EnerGov's search is backed by Elasticsearch with index.max_result_window = 10000,
so offset paging dies at from+size > 10000 (page 101 @ size 100). The SPA's
default global search (SearchModule=1) also IGNORES filters, so it can't be
narrowed. The fix (the cu-permits year-chunking doctrine, ported to JSON):

  * use SearchModule=2 (Permit-specific search), which honors PermitCriteria
    filters including ApplyDateFrom/To;
  * chunk by ApplyDate year (San Carlos: 1999..present, ~2k permits/year — well
    under 10k); recursively halve any window that still exceeds the cap;
  * page through each window and cache its pages;
  * step 1 dedups on the CaseId GUID, so overlapping windows are free.

No Cloudflare gate (IIS host) — the only resilience is HTTP 429/5xx backoff.
"""

from __future__ import annotations

import datetime as dt
import math
import time
from pathlib import Path

import requests

from utils.auth import search_headers
from utils.config import DEFAULT_PAGE_SIZE, SEARCH_URL, build_search_body
from utils.io import atomic_write_json, load_json


class SearchError(RuntimeError):
    """Non-retryable search failure (bad body, persistent 5xx, etc.)."""


# 401 is a transient server blip, not an auth requirement: this search is
# anonymous (the SPA's own public call), and EnerGov intermittently returns 401
# "Authorization has been denied" under load even on records/queries it serves
# anonymously a moment later (confirmed against detail GETs 2026-05-29). Retrying
# the SAME anonymous request rides through it — no credentials are ever added.
# 500/502/503/504 (the whole 5xx-transient family) are added because EnerGov has
# been observed in all of these states during a deploy / pool restart. The
# 2026-05-30 03:10 outage flapped 500 -> 502 as it recovered. A brief 5xx
# self-heals; the retry rides it through and a sustained outage still bubbles
# up as a SearchError after exhausting retries (bounded), where the tick's
# outage-backoff then prevents wasted CPU on subsequent fires.
RETRY_STATUS = {401, 429, 500, 502, 503, 504}   # transient — back off and retry
MAX_RETRIES = 4
BACKOFF_BASE = 1.0               # seconds: 1, 2, 4, 8
RESULT_WINDOW_CAP = 10000        # Elasticsearch index.max_result_window
SEARCH_MODULE_MODULE_SPECIFIC = 2  # honors PermitCriteria filters + paging


def make_session() -> requests.Session:
    s = requests.Session()
    s.headers.update(search_headers())
    return s


def _request(session: requests.Session, body: dict, what: str) -> dict:
    """POST a search body; return the `Result` envelope. Retries 429/503."""
    last: Exception | None = None
    for attempt in range(MAX_RETRIES + 1):
        try:
            r = session.post(SEARCH_URL, json=body, timeout=60)
        except requests.RequestException as exc:
            last = exc
        else:
            if r.status_code == 200:
                try:
                    payload = r.json()
                except ValueError as exc:
                    # A 200 with a non-JSON body (truncated / HTML error page
                    # from the flaky IIS host) is a transient blip -> retry like
                    # a 5xx rather than letting the ValueError escape _request
                    # and abort step0 (critical=True -> whole tick aborts + a
                    # spurious outage backoff). The whole point of this loop is
                    # to ride through blips; an unparseable 200 is one.
                    last = SearchError(f"HTTP 200 unparseable body: {str(exc)[:80]}")
                else:
                    if not payload.get("Success", True):
                        raise SearchError(f"{what}: Success=false "
                                          f"err={str(payload.get('ErrorMessage'))[:200]!r}")
                    result = payload.get("Result")
                    if result is None:
                        raise SearchError(f"{what}: no Result envelope")
                    return result
            elif r.status_code not in RETRY_STATUS:
                raise SearchError(f"{what}: HTTP {r.status_code} body={r.text[:200]!r}")
            else:
                last = SearchError(f"HTTP {r.status_code}")
        if attempt < MAX_RETRIES:
            time.sleep(BACKOFF_BASE * (2 ** attempt))
    raise SearchError(f"{what}: exhausted retries ({last})")


def _iso_from(d: dt.date) -> str:
    return f"{d.isoformat()}T00:00:00"


def _iso_to(d: dt.date) -> str:
    return f"{d.isoformat()}T23:59:59"


def _label(d0: dt.date, d1: dt.date) -> str:
    if d0.month == 1 and d0.day == 1 and d1.month == 12 and d1.day == 31 \
            and d0.year == d1.year:
        return str(d0.year)
    return f"{d0:%Y%m%d}-{d1:%Y%m%d}"


def count_window(session: requests.Session, filter_module: int, sort_by: str,
                 date_from: str | None, date_to: str | None) -> int:
    body = build_search_body(filter_module, 1, 1, sort_by=sort_by,
                             search_module=SEARCH_MODULE_MODULE_SPECIFIC,
                             apply_date_from=date_from, apply_date_to=date_to)
    return int(_request(session, body, "count").get("TotalFound") or 0)


def _fetch_window(session, filter_module, sort_by, d0, d1, raw_dir, page_size,
                  page_delay, no_cache, audit, log) -> None:
    """Fetch one date window [d0, d1], recursively splitting if it exceeds the
    10k cap. Caches pages under raw_dir/<label>/page_NNN.json."""
    date_from, date_to = _iso_from(d0), _iso_to(d1)
    label = _label(d0, d1)
    count = count_window(session, filter_module, sort_by, date_from, date_to)
    if count == 0:
        return
    if count >= RESULT_WINDOW_CAP:
        if (d1 - d0).days <= 0:
            # Single day still over the cap — fetch what we can (the cap clips it)
            # and record it so it's visible in the audit.
            log(f"  [{label}] WARNING: {count} >= cap on a single day; "
                f"capped at {RESULT_WINDOW_CAP}")
        else:
            mid = d0 + (d1 - d0) // 2
            log(f"  [{label}] {count} >= {RESULT_WINDOW_CAP} — splitting at {mid}")
            _fetch_window(session, filter_module, sort_by, d0, mid, raw_dir,
                          page_size, page_delay, no_cache, audit, log)
            _fetch_window(session, filter_module, sort_by, mid + dt.timedelta(days=1),
                          d1, raw_dir, page_size, page_delay, no_cache, audit, log)
            return

    pages = math.ceil(count / page_size)
    audit["windows"].append({"label": label, "count": count, "pages": pages})
    audit["sum_window_counts"] += count
    win_dir = raw_dir / label
    win_dir.mkdir(parents=True, exist_ok=True)
    for p in range(1, pages + 1):
        dest = win_dir / f"page_{p:03d}.json"
        if dest.exists() and not no_cache:
            audit["pages_skipped"] += 1
            # Count the cached page's records too, so `reconciled`
            # (records_seen == sum_window_counts) stays meaningful on a warm
            # cache. Without this a plain re-run over cached pages always trips
            # the "records_seen != sum_window_counts" anomaly warning even
            # though every promised record is already on disk. An unreadable
            # cache file is left uncounted, so reconciliation correctly flags it.
            try:
                cached = load_json(dest, None)
            except (OSError, ValueError):
                cached = None
            if cached is not None:
                audit["records_seen"] += len(cached.get("EntityResults") or [])
            continue
        body = build_search_body(filter_module, p, page_size, sort_by=sort_by,
                                 search_module=SEARCH_MODULE_MODULE_SPECIFIC,
                                 apply_date_from=date_from, apply_date_to=date_to)
        result = _request(session, body, f"{label} p{p}")
        atomic_write_json(dest, result)
        audit["pages_fetched"] += 1
        audit["records_seen"] += len(result.get("EntityResults") or [])
        if p < pages:
            time.sleep(page_delay)
    log(f"  [{label}] {count} records over {pages} page(s)")


def fetch_all(module_label: str, filter_module: int, sort_by: str,
              raw_dir: Path, start_year: int, end_year: int,
              page_size: int = DEFAULT_PAGE_SIZE, page_delay: float = 1.0,
              no_cache: bool = False, log=print) -> dict:
    """Year-chunk the full result set across [start_year, end_year] and cache
    each window's pages. Returns an audit dict including a reconciliation of the
    summed per-window counts against the global TotalFound."""
    raw_dir.mkdir(parents=True, exist_ok=True)
    session = make_session()
    started = time.monotonic()
    audit = {
        "module": module_label, "page_size": page_size,
        "start_year": start_year, "end_year": end_year,
        "global_total": None, "sum_window_counts": 0,
        "windows": [], "pages_fetched": 0, "pages_skipped": 0,
        "records_seen": 0, "errors": [],
    }
    try:
        # The global count is "nice to have" — it's only used for the
        # full_coverage reconciliation flag. If the portal is sustained-500
        # on the global query (observed 2026-05-30) but might still answer
        # year-windowed queries, we shouldn't abort the whole refresh on
        # this. Record the error in the audit and proceed to the windows.
        try:
            audit["global_total"] = count_window(session, filter_module,
                                                 sort_by, None, None)
            log(f"  global TotalFound={audit['global_total']}; "
                f"chunking ApplyDate years {start_year}..{end_year}")
        except SearchError as exc:
            audit["errors"].append({"phase": "global_count", "error": str(exc)})
            log(f"  [global count] ERROR: {exc}; continuing with year windows")
        for year in range(start_year, end_year + 1):
            try:
                _fetch_window(session, filter_module, sort_by,
                              dt.date(year, 1, 1), dt.date(year, 12, 31),
                              raw_dir, page_size, page_delay, no_cache, audit, log)
            except SearchError as exc:
                audit["errors"].append({"year": year, "error": str(exc)})
                log(f"  [{year}] ERROR: {exc}")
    finally:
        session.close()
    audit["duration_seconds"] = round(time.monotonic() - started, 1)
    # full_coverage: did we ask the API about EVERY permit in the portal?
    # True only for a complete-history backfill that includes the global total.
    audit["full_coverage"] = (audit["sum_window_counts"] == audit["global_total"])
    # reconciled: of the records the API SAID exist in the windows we asked
    # about, did we actually receive them all? This is the integrity check
    # that matters for both partial refreshes (e.g. the throttled tick's 2y
    # window) and full backfills -- 'False' here is a real anomaly to chase.
    audit["reconciled"] = (audit["records_seen"] == audit["sum_window_counts"])
    return audit

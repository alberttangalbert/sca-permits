"""Step 0 fetch engine — paged JSON search against EnerGov CSS.

Replaces cu-permits' Accela requests-postback + ViewState machinery with a plain
paged JSON POST. There is no Cloudflare gate (IIS host), so the only resilience
needed is HTTP 429/5xx backoff + a consecutive-error abort budget.

Each page's raw response JSON is cached to outputs/raw/sca/<module>/page_NNNNN.json
so step 1 re-parses are free and there's an audit trail (same discipline as the
Accela cities caching result-grid HTML).
"""

from __future__ import annotations

import time
from pathlib import Path

import requests

from utils.auth import search_headers
from utils.config import DEFAULT_PAGE_SIZE, SEARCH_URL, build_search_body
from utils.io import atomic_write_json


class SearchError(RuntimeError):
    """Non-retryable search failure (bad body, persistent 5xx, etc.)."""


# 429/503 are transient (rate-limit / unavailable); back off and retry.
RETRY_STATUS = {429, 503}
MAX_RETRIES = 4
BACKOFF_BASE = 1.0          # seconds: 1, 2, 4, 8
# A run aborts if this many pages fail in a row (defensive, mirrors cu-permits).
MAX_CONSECUTIVE_ERRORS = 5


def make_session() -> requests.Session:
    s = requests.Session()
    s.headers.update(search_headers())
    return s


def search_page(session: requests.Session, filter_module: int,
                page_number: int, page_size: int,
                sort_by: str, sort_ascending: bool) -> dict:
    """POST one search page; return the parsed `Result` envelope.

    Retries 429/503 with exponential backoff. Raises SearchError on a
    non-retryable failure or after exhausting retries.
    """
    body = build_search_body(filter_module, page_number, page_size,
                             sort_by=sort_by, sort_ascending=sort_ascending)
    last_exc: Exception | None = None
    for attempt in range(MAX_RETRIES + 1):
        try:
            r = session.post(SEARCH_URL, json=body, timeout=60)
        except requests.RequestException as exc:
            last_exc = exc
        else:
            if r.status_code == 200:
                payload = r.json()
                if not payload.get("Success", True):
                    raise SearchError(
                        f"page {page_number}: Success=false "
                        f"err={payload.get('ErrorMessage')!r}")
                result = payload.get("Result")
                if result is None:
                    raise SearchError(f"page {page_number}: no Result envelope")
                return result
            if r.status_code not in RETRY_STATUS:
                raise SearchError(
                    f"page {page_number}: HTTP {r.status_code} "
                    f"body={r.text[:200]!r}")
            last_exc = SearchError(f"HTTP {r.status_code}")
        if attempt < MAX_RETRIES:
            backoff = BACKOFF_BASE * (2 ** attempt)
            time.sleep(backoff)
    raise SearchError(f"page {page_number}: exhausted retries ({last_exc})")


def page_path(raw_dir: Path, page_number: int) -> Path:
    return raw_dir / f"page_{page_number:05d}.json"


def fetch_all(module_label: str, filter_module: int, sort_by: str,
              raw_dir: Path, page_size: int = DEFAULT_PAGE_SIZE,
              max_pages: int | None = None, page_delay: float = 1.0,
              no_cache: bool = False, sort_ascending: bool = True,
              log=print) -> dict:
    """Page through the entire result set, caching each page's JSON.

    Returns an audit dict: total_found, total_pages, pages_fetched,
    pages_skipped (already cached), records_seen, duration_seconds, errors.
    """
    raw_dir.mkdir(parents=True, exist_ok=True)
    session = make_session()
    started = time.monotonic()
    audit = {
        "module": module_label, "filter_module": filter_module,
        "page_size": page_size, "total_found": None, "total_pages": None,
        "pages_fetched": 0, "pages_skipped": 0, "records_seen": 0,
        "errors": [],
    }
    consecutive_errors = 0
    try:
        # Page 1 first to learn TotalPages.
        first = search_page(session, filter_module, 1, page_size,
                            sort_by, sort_ascending)
        total_pages = int(first.get("TotalPages") or 0)
        total_found = int(first.get("TotalFound") or 0)
        audit["total_found"] = total_found
        audit["total_pages"] = total_pages
        last_page = total_pages if max_pages is None else min(total_pages, max_pages)
        log(f"  TotalFound={total_found} TotalPages={total_pages} "
            f"-> fetching {last_page} page(s) @ size {page_size}")

        for page in range(1, last_page + 1):
            dest = page_path(raw_dir, page)
            if page == 1:
                result = first
            elif dest.exists() and not no_cache:
                audit["pages_skipped"] += 1
                continue
            else:
                try:
                    result = search_page(session, filter_module, page, page_size,
                                        sort_by, sort_ascending)
                    consecutive_errors = 0
                except SearchError as exc:
                    consecutive_errors += 1
                    audit["errors"].append({"page": page, "error": str(exc)})
                    log(f"  [page {page}] ERROR: {exc}")
                    if consecutive_errors >= MAX_CONSECUTIVE_ERRORS:
                        log(f"  aborting: {consecutive_errors} consecutive errors")
                        break
                    time.sleep(page_delay)
                    continue

            atomic_write_json(dest, result)
            audit["pages_fetched"] += 1
            audit["records_seen"] += len(result.get("EntityResults") or [])
            if page % 25 == 0 or page == last_page:
                log(f"  page {page}/{last_page}  (records_seen={audit['records_seen']})")
            if page < last_page:
                time.sleep(page_delay)
    finally:
        session.close()
    audit["duration_seconds"] = round(time.monotonic() - started, 1)
    return audit

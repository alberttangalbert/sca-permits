"""Step 2 fetch engine — per-record detail GETs against EnerGov CSS.

Each record's full detail is a single anonymous, stateless GET:

    GET …/api/energov/permits/permit/<CaseId>   -> {"Result": {...}}

No session seeding, no postbacks, no Cloudflare gate (IIS host) — so detail
fetches are embarrassingly parallel-safe, but we stay single-threaded and polite
(the cu-permits doctrine). The only resilience needed is HTTP 429/5xx backoff.

Pages are cached to raw_dir/<case_id>.json; re-runs skip cached files unless
no_cache, so a backfill is fully resumable.
"""

from __future__ import annotations

import time
from pathlib import Path

import requests

from utils.auth import search_headers
from utils.config import permit_detail_url
from utils.io import atomic_write_json


class DetailError(RuntimeError):
    """Non-retryable detail failure (404, persistent 5xx, bad body)."""


# 401 here is a TRANSIENT server blip, not an auth requirement: the detail GET is
# anonymous, and on 2026-05-29 four consecutive records returned 401
# ("Authorization has been denied for this request.") mid-backfill yet returned
# 200 on an immediate re-GET. Retrying the SAME anonymous request rides through
# the blip — it never adds credentials (we stay anonymous-only). 500 added for
# the same reason after a 2026-05-30 portal outage took search down with
# "An error has occurred." for several minutes. A record that is genuinely
# restricted (or persistently 500'd) would still error after exhausting retries,
# so this can't loop forever.
RETRY_STATUS = {401, 429, 500, 502, 503, 504}   # transient — back off and retry
MAX_RETRIES = 4
BACKOFF_BASE = 1.0          # seconds: 1, 2, 4, 8


def make_session() -> requests.Session:
    s = requests.Session()
    s.headers.update(search_headers())
    return s


def fetch_one(session: requests.Session, case_id: str) -> dict:
    """GET one record's detail; return its `Result` envelope. Retries 429/503."""
    url = permit_detail_url(case_id)
    last: Exception | None = None
    for attempt in range(MAX_RETRIES + 1):
        try:
            r = session.get(url, timeout=60)
        except requests.RequestException as exc:
            last = exc
        else:
            if r.status_code == 200:
                try:
                    payload = r.json()
                except ValueError as exc:
                    # A 200 with a non-JSON body (truncated response / HTML error
                    # page from the flaky IIS host) is a transient blip, not a
                    # permanent failure. Retry it like a 5xx -- letting the
                    # ValueError escape would crash the whole backfill, since
                    # fetch_details only catches DetailError, not ValueError.
                    last = DetailError(f"HTTP 200 unparseable body: {str(exc)[:80]}")
                else:
                    if not payload.get("Success", True):
                        raise DetailError(f"{case_id}: Success=false "
                                          f"err={str(payload.get('ErrorMessage'))[:200]!r}")
                    result = payload.get("Result")
                    if result is None:
                        raise DetailError(f"{case_id}: no Result envelope")
                    return result
            elif r.status_code not in RETRY_STATUS:
                raise DetailError(f"{case_id}: HTTP {r.status_code} "
                                  f"body={r.text[:160]!r}")
            else:
                last = DetailError(f"HTTP {r.status_code}")
        if attempt < MAX_RETRIES:
            time.sleep(BACKOFF_BASE * (2 ** attempt))
    raise DetailError(f"{case_id}: exhausted retries ({last})")


def fetch_details(case_ids: list[str], raw_dir: Path, page_delay: float = 0.3,
                  no_cache: bool = False, log=print) -> dict:
    """Fetch + cache detail JSON for each case_id. Returns an audit dict.

    Cache layout: raw_dir/<case_id>.json (one file per record). Already-cached
    records are skipped unless no_cache, making the backfill resumable."""
    raw_dir.mkdir(parents=True, exist_ok=True)
    session = make_session()
    started = time.monotonic()
    audit = {
        "requested": len(case_ids), "fetched": 0, "skipped": 0,
        "errors": [], "page_delay": page_delay,
    }
    try:
        total = len(case_ids)
        for i, case_id in enumerate(case_ids, 1):
            dest = raw_dir / f"{case_id}.json"
            if dest.exists() and not no_cache:
                audit["skipped"] += 1
                continue
            try:
                result = fetch_one(session, case_id)
            except DetailError as exc:
                audit["errors"].append({"case_id": case_id, "error": str(exc)})
                log(f"  [{i}/{total}] ERROR {exc}")
                continue
            atomic_write_json(dest, result)
            audit["fetched"] += 1
            if audit["fetched"] % 250 == 0:
                log(f"  [{i}/{total}] fetched {audit['fetched']} "
                    f"(skipped {audit['skipped']}, errors {len(audit['errors'])})")
            time.sleep(page_delay)
    finally:
        session.close()
    audit["duration_seconds"] = round(time.monotonic() - started, 1)
    return audit

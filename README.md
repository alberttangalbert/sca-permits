# scar-permits

Residential-construction **lead funnel for a GC**, built on the City of San Carlos
permit portal. Sibling to `cu-permits` (Cupertino) — same pipeline shape and lead
scoring, but San Carlos runs **Tyler EnerGov Citizen Self Service (CSS)**, a JSON
REST API, instead of Accela's HTML/WebForms. So steps 0–2 are a clean JSON rewrite;
the scoring model (step 3) and data philosophy carry over. See `docs/handoff/`.

## Status

This build covers **step 0 (search) + step 1 (parse)** for the **Permit** module:
page through the full public result set and build a deduped permit table keyed on
the EnerGov record GUID. Detail enrichment (step 2), scoring (step 3), and D1 sync
(step 4) are scaffolded in the handoff docs and come next.

## The API (verified 2026-05-26, read-only recon)

- **Endpoint:** `POST …/apps/selfservice/api/energov/search/search` (plain ASP.NET
  Web API, IIS, **no Cloudflare**).
- **Anonymous** — public search returns HTTP 200 with **no login / no token**.
- **Required headers:** `Accept`, `Content-Type: application/json`, and
  **`tenantId: 1`** (omitting `tenantId` → opaque HTTP 500 — this was the one gap
  the handoff docs flagged). `tenantName` / `Tyler-TenantUrl` / `Tyler-Tenant-Culture`
  are sent for parity but aren't required.
- **Body:** `SearchModule: 1` (All) + `FilterModule: 2` (Permit); top-level
  `PageNumber`/`PageSize` (allowed 10/25/50/100); type/status filters use the
  `"none"` sentinel. We pull everything and filter downstream (no server-side
  filtering), so a scoring change never forces a re-scrape.
- **Response:** `Result.EntityResults[]`, `Result.TotalFound`, `Result.TotalPages`.
  Each record carries **`CaseId`** (GUID — the step-2 detail key), `CaseNumber`,
  `CaseType`, `CaseStatus`, `ApplyDate`, `AddressDisplay`, `MainParcel`, `Description`.
- As of recon, the Permit corpus is **~52,931 records** — far more than "post-cutover
  only," so CSS clearly holds migrated history (good for backfill).

## Setup

```bash
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
python3 src/init_db.py
```

## Run

```bash
# Step 0 — page through the search API, caching each page's JSON.
python3 src/step0_fetch_search_results.py --module Permit            # full (~530 pages @ size 100)
python3 src/step0_fetch_search_results.py --module Permit --max-pages 2   # smoke test

# Step 1 — parse cached JSON into sca_permits (dedup on case_id; safe to re-run).
python3 src/step1_parse_search_results.py --module Permit
```

Cached pages: `outputs/raw/sca/permit/page_NNNNN.json`. Audit ledgers:
`outputs/step_{0,1}/runs_permit.json`. Database: `outputs/sca_permits.db`.

## Layout

```
src/
  init_db.py                      apply migrations
  step0_fetch_search_results.py   paged JSON search client  (entrypoint)
  step1_parse_search_results.py   JSON -> sca_permits        (entrypoint)
  utils/
    config.py    API URL, headers, FilterModule enum, search-body builder
    auth.py      anonymous headers (+ optional SCA_BEARER_TOKEN fallback)
    io.py        SQLite connect (WAL) + atomic writes + migrations  (from cu-permits)
    normalize.py address canonicalization for clustering            (from cu-permits)
    step_0/fetch.py   paged POST + page caching + 429 backoff
    step_1/parsing.py EntityResults[] -> row dicts
migrations/0001_init_sca_permits.sql   sca_permits table
docs/handoff/                          design notes (platform, API, scoring)
```

## Notes

- **No proxy, no Selenium, no Cloudflare machinery** — a polite single-IP `requests`
  client is correct here. Step 0 sleeps ~1s between pages.
- Auth is anonymous; `SCA_BEARER_TOKEN` in `.env` is a fallback only (see `.env.example`),
  used if the portal ever stops serving anonymous requests. The scraper never logs in.
- Tables use the `sca_` prefix (per the handoff bundle).

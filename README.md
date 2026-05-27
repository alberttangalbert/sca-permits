# scar-permits

Residential-construction **lead funnel for a GC**, built on the City of San Carlos
permit portal. Sibling to `cu-permits` (Cupertino) — same pipeline shape and lead
scoring, but San Carlos runs **Tyler EnerGov Citizen Self Service (CSS)**, a JSON
REST API, instead of Accela's HTML/WebForms. So steps 0–2 are a clean JSON rewrite;
the scoring model (step 3) and data philosophy carry over. See `docs/handoff/`.

## Status

This build covers **steps 0–2** for the **Permit** module:

- **Step 0/1 — search + parse:** page through the full public result set
  (year-chunked to beat the Elasticsearch 10k cap) and build a deduped permit
  table keyed on the EnerGov record GUID. **52,933 permits loaded** (1999–present).
- **Step 2 — detail enrichment:** one anonymous GET per record adds the fields
  search omits — **valuation** (the size signal), **owner/applicant/contractor
  contacts** (names, company, email, phone — public!), parcel/APN, and holds.

Scoring (step 3) and D1 sync (step 4) are scaffolded in the handoff docs and come
next.

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

### The 10k cap and year-chunking (step 0)

The search is Elasticsearch-backed with `index.max_result_window = 10000`, and the
SPA's default `SearchModule: 1` **ignores per-module filters** — so naive offset
paging dies at page 101 and can never reach all 52,933 records. Step 0 instead uses
**`SearchModule: 2`** (which honors `PermitCriteria.ApplyDateFrom/To`) and chunks by
**ApplyDate year** (~2k/year, well under the cap), recursively halving any window
that still exceeds it. The summed per-window counts are reconciled against the
global `TotalFound`, and step 1 dedups on `CaseId`, so overlapping windows are free.

### Detail API (step 2, verified 2026-05-27)

- **Endpoint:** `GET …/api/energov/permits/permit/<CaseId>` — anonymous, stateless,
  HTTP 200. (The sibling `permits/permit?id=<CaseId>` returns resolved type/status
  *names* + dates, but we already have those from search, so it's unused — one GET.)
- **Returns:** `ValuationValue` (numeric project cost — Cupertino had none),
  `SquareFeet` (usually 0, so valuation is the real size signal), and embedded
  `Contacts[]` / `Addresses[]` / `Parcels[]` / `Holds[]`.
- **Contacts are public** for anonymous users (verified across samples) — role
  (`Owner`/`Applicant`/`Contractor`/`Architect`/`Engineer`/`Agent`), name, company,
  email, phone. This makes San Carlos the richest lead source of the cities so far.

## Setup

```bash
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
python3 src/init_db.py
```

## Run

```bash
# Step 0 — page through the search API (year-chunked), caching each page's JSON.
python3 src/step0_fetch_search_results.py --module Permit                  # full backfill (1999..now)
python3 src/step0_fetch_search_results.py --module Permit --start-year 2026   # smoke test (one year)

# Step 1 — parse cached JSON into sca_permits (dedup on case_id; safe to re-run).
python3 src/step1_parse_search_results.py --module Permit

# Step 2 — enrich a slice with detail (valuation + contacts + parcel + holds).
python3 src/step2_fetch_details.py --start-year 2025          # fetch detail JSON for recent records
python3 src/step2_fetch_details.py --start-year 2026 --limit 25   # smoke test
python3 src/step2_fetch_details.py --all                     # full historical backfill (~52k GETs, long)
python3 src/step2_parse_details.py                           # cached detail -> detail + contacts tables
```

`step2_fetch_details.py` requires a selection filter (`--all`, `--start-year`,
`--since`, `--status`, `--type-like`, or `--limit`) — it won't fetch all 52k
implicitly. Both step 2 scripts are idempotent; the cache makes the backfill
resumable.

Cached search pages: `outputs/raw/sca/permit/<year>/page_NNN.json`. Cached detail:
`outputs/raw/sca/permit_detail/<case_id>.json`. Audit ledgers under
`outputs/step_{0,1,2}/`. Database: `outputs/sca_permits.db`.

## Layout

```
src/
  init_db.py                      apply migrations
  step0_fetch_search_results.py   paged JSON search client   (entrypoint)
  step1_parse_search_results.py   JSON -> sca_permits         (entrypoint)
  step2_fetch_details.py          per-record detail GETs      (entrypoint)
  step2_parse_details.py          detail JSON -> detail+contacts (entrypoint)
  utils/
    config.py    API URLs, headers, FilterModule enum, search-body builder
    auth.py      anonymous headers (+ optional SCA_BEARER_TOKEN fallback)
    io.py        SQLite connect (WAL) + atomic writes + migrations  (from cu-permits)
    normalize.py address canonicalization for clustering            (from cu-permits)
    step_0/fetch.py     year-chunked paged POST + caching + 429 backoff
    step_1/parsing.py   EntityResults[] -> row dicts
    step_2/detail.py    per-record GET + caching + 429 backoff
    step_2/parsing.py   detail JSON -> detail row + contact rows (role normalize)
migrations/0001_init_sca_permits.sql    sca_permits table
migrations/0002_add_permit_detail.sql   sca_permit_detail + sca_permit_contacts
docs/handoff/                           design notes (platform, API, scoring)
```

## Notes

- **No proxy, no Selenium, no Cloudflare machinery** — a polite single-IP `requests`
  client is correct here. Step 0 sleeps ~1s between pages.
- Auth is anonymous; `SCA_BEARER_TOKEN` in `.env` is a fallback only (see `.env.example`),
  used if the portal ever stops serving anonymous requests. The scraper never logs in.
- Tables use the `sca_` prefix (per the handoff bundle).

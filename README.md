# scar-permits

Residential-construction **lead funnel for a GC**, built on the City of San Carlos
permit portal. Sibling to `cu-permits` (Cupertino) — same pipeline shape and lead
scoring, but San Carlos runs **Tyler EnerGov Citizen Self Service (CSS)**, a JSON
REST API, instead of Accela's HTML/WebForms. So steps 0–2 are a clean JSON rewrite;
the scoring model (step 3) and data philosophy carry over. See `docs/handoff/`.

## Status

This build covers **steps 0–4** for the **Permit** module:

- **Step 0/1 — search + parse:** page through the full public result set
  (year-chunked to beat the Elasticsearch 10k cap) and build a deduped permit
  table keyed on the EnerGov record GUID. **~53k permits loaded** (1999–present).
- **Step 2 — detail enrichment:** one anonymous GET per record adds the fields
  search omits — **valuation** (the size signal), **owner/applicant/contractor/
  architect/designer/engineer contacts** (names, company, email, phone — public!),
  parcel/APN, and holds. Backfill horizon: 2020-present (default; see
  `tick.py --backfill-since`). **~46k records** enriched at last sync.
- **Step 3 — lead scoring:** rank each enriched permit into a banded lead
  (`HIGH`/`MEDIUM`/`LOW`/`DROP`) and denormalize the best contact (and the role
  it came from — see "Outreach contact selection" below) plus contractor info
  for outreach. Of ~46k enriched records, post-2020 typically has **56 HIGH +
  130 MEDIUM + ~290 LOW** — the actionable funnel of new-SFR / ADU / addition
  projects that are approved-or-near but pre-contractor.

- **Step 3b — clustering:** fold a parcel's permits into one project so the GC
  doesn't call the same owner once per permit. Clustering key precedence:
  `main_parcel` (PARCEL) → `address_norm` (ADDRESS) → `case_id` (SINGLETON).
  Detail-row `MainParcel` is used when the search row's is NULL, and a
  per-address parcel-canonicalization pass adopts a sibling permit's parcel
  for permits at addresses with exactly one observed parcel. Measured:
  ~46k leads collapse to ~10k projects. `sca_lead_clusters` is the deduped
  call list (one row per project, anchored on the strongest permit;
  contact info from the anchor when reachable, falling through to siblings
  otherwise — useful because the LYNCH-trust-style cases find historical
  contact info on a 2007 sibling for a 2024 anchor).
- **Step 4 — D1 sync:** publish to the **shared `permits` D1** (the `sca_`
  prefix keeps the tables clear of the other cities'): `sca_leads` (per-permit,
  default) and `sca_lead_clusters` (deduped projects, `--clusters`). Defaults to
  generating portable SQL locally; the remote push is opt-in and uses your own
  credentials (rows carry homeowner PII, so it never pushes without them). The
  sync is a **mirror** (`--prune`, on in the tick): leads that left the
  actionable set — e.g. a permit that got issued/completed and dropped below the
  threshold — are deleted from D1 rather than left behind as stale rows.

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

### Lead scoring (step 3, no scraping)

A multiplicative **gates × factors** model (the cu-permits architecture,
re-derived for San Carlos's real type/status vocabulary):

```
lead_score = type_fit · size_factor · status_factor
                 · contractor_factor · hold_factor · recency_factor
```

The score is the pure product of the six 0–1 factors, so it lives in `[0, 1]`
(the canonical range shared with the sibling cities).

- **`type_fit`** (`src/utils/step_3/type_fit_rules.json`) — ordered substring
  match on `case_type`: new SFR / ADU / second-unit = 1.0, addition = 0.9,
  interior remodel = 0.75, accessory structure (detached studio / patio cover) =
  0.7, sub-trades (solar, reroof, HVAC, electrical…) ≈ 0.1, commercial = 0.2. The
  `category` tag is coarse (NEW_SFR / ADU / ADDITION / REMODEL / ACCESSORY /
  COMMERCIAL / SUBTRADE / OTHER); `type_fit` is the score. A description-keyword
  pass upgrades the broad "Miscellaneous" **and ACCESSORY** buckets when the text
  names an ADU / addition / new dwelling (so a detached structure described as a
  new ADU still scores as one). A separate description-prefix override
  (`^VOID|WRONG PERMIT TYPE|...`) forces DEAD status when a city worker
  void-marked the description but left the case_status stale.
- **`size_factor`** — bucketed from EnerGov `ValuationValue` (the size signal
  Cupertino lacked); a missing/0 valuation is neutral, not zero.
- **`status_factor`** — the near-issuance window scores highest (`Approved`,
  `Fees Due`, `Fees Paid`); `Finaled` / `Expired` / `Cancelled` ≈ 0.
- **`contractor_factor`** — a permit with **no contractor attached** is the
  better lead (homeowner hasn't engaged one yet) → 1.0, else 0.8.
- **`hold_factor`** — an active non-expired hold means the project is stuck at
  the city; lightly de-prioritized (0.9) and flagged for the caller.
- **`recency_factor`** — soft decay with `apply_date` age (≤1y 1.0 / 1-2y 0.8 /
  2-3y 0.55 / 3-5y 0.3 / >5y 0.1). Demotes migrated pre-cutover records frozen
  at `Approved` (28% of the actionable funnel was >5y dead before this).

Banded `HIGH ≥ 0.50`, `MEDIUM ≥ 0.22`, `LOW ≥ 0.07`, else `DROP`. Results land in
`sca_leads` with the best owner/contractor contact denormalized for outreach.
Re-scoring is a seconds-long re-run (`--rebuild`), never a re-fetch.

### Outreach contact selection (step 3)

The lead's surfaced contact is picked by walking a six-tier reachability chain
on the permit's `Contacts[]`:

```
OWNER -> APPLICANT -> ARCHITECT -> DESIGNER -> ENGINEER -> AGENT
```

The first tier whose best candidate has email OR phone wins. `contact_role` on
`sca_leads` (migration 0007) labels which tier was picked so the GC's call list
renders "Name (Architect)" instead of mislabeling the architect as the
homeowner. Migration 0008 propagates the same field to `sca_lead_clusters`.

Three families of source-data placeholders are dropped from the candidate pool
BEFORE the chain runs: `void void` (redacted-applicant marker), `BUILDER OWNER`
(owner-builder generic stamp + placeholder 925-area phone), and
`EnerGov YYYYQN` (Tyler migration / sync stamp paired with the
`energovconversion@tylertech.com` email). Typo emails (multiple `@`'s, no `.`)
and sub-10-digit phones are NULL'd before the ranking so garbage can't beat a
real lower-tier candidate.

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
python3 src/step2_parse_details.py                           # parse ALL cached detail -> detail + contacts tables
python3 src/step2_parse_details.py --missing-only            # parse only un-parsed files (incremental; what the tick uses)

# Step 3 — score enriched permits into banded leads (no scraping; safe to re-run).
python3 src/step3_score.py --rebuild                         # score everything enriched (then re-run step3b — see below)
python3 src/step3_score.py --dry-run                         # report band/category mix, no writes

# Step 3b — cluster scored leads into one-row-per-project (the deduped call list).
python3 src/step3b_cluster.py                                # build sca_lead_clusters
python3 src/step3b_cluster.py --dry-run                      # report collapse stats, no writes

# Step 4 — sync to the shared `permits` D1 (generates SQL by default).
python3 src/step4_sync_d1.py                                 # per-permit leads -> d1_sync.sql
python3 src/step4_sync_d1.py --clusters                      # deduped projects -> d1_clusters_sync.sql
python3 src/step4_sync_d1.py --prune                         # ...mirror: also delete leads that left the actionable set
CF_ACCOUNT_ID=… CF_D1_DATABASE_ID=<shared permits D1> CF_API_TOKEN=… \
  python3 src/step4_sync_d1.py --clusters --execute          # push via D1 HTTP API (your creds)
```

### Keeping it fresh — the incremental tick

A lead is only worth calling while it's live, so `src/tick.py` runs the whole
loop incrementally, on **two cadences**:

- **Search re-pull (throttled, default every 6h):** re-pull the recent ApplyDate
  year-windows (status — the score-critical field — rides on the search row, so
  this catches every status transition), parse, and fetch detail **only for
  newly-filed permits** (cache-skips the rest). `--min-interval-hours` guards the
  live portal so a frequent scheduler can't hammer it.
- **Stale-status refresh (step 0b, on every search re-pull):** the year-window
  re-pull only covers recent years, so an actionable lead filed earlier (e.g. a
  2024 permit frozen at "Approved" the city has since Issued) would keep a stale
  status. step 0b re-pulls the narrow **ApplyDate day-window** of every current
  HIGH/MEDIUM lead filed before the refresh floor — one cheap day-window per
  distinct filing day — so step 1 upserts their fresh status and the re-score
  drops any that went dead. `--limit-days` bounds it; it bails after a few
  consecutive portal errors (rate-limit politeness); `--skip-stale-refresh` off.
- **Historical detail backfill (every fire):** enrich a `--backfill-chunk`
  (default 200, newest-missing first) of the ~50k older permits that are still
  search-only, until the whole history has detail. It self-quiesces when nothing
  is missing, and a fully-throttled, fully-backfilled tick skips without even
  taking the lock.

Both feed the same re-parse → re-score → re-cluster → D1-sync tail. The tail's
detail parse is incremental (`--missing-only`), so a fire ingests just the
freshly-fetched chunk instead of re-parsing the whole (growing) cache each time;
re-parse the full history with a manual no-flag `step2_parse_details.py` after a
parser change.

```bash
python3 src/tick.py --dry-run        # print the plan, touch nothing
python3 src/tick.py                  # refresh (if due) + backfill 200 (SQL-only sync)
python3 src/tick.py --backfill-chunk 500   # enrich more history per fire
python3 src/tick.py --backfill-chunk 0     # refresh only, no backfill
python3 src/tick.py --execute-sync   # ...and push to D1 (needs CF_* env)
scripts/tick.sh                      # same, but venv-activate + tee to logs/
python3 src/healthcheck.py           # read-only integrity check (tick runs this last)
python3 -m unittest discover -s tests   # regression tests for the pure logic (stdlib only)
```

Schedule it from cron, **staggered** off the sibling cities (Fremont 4:30 /
Santa Clara 5:30 / Cupertino 6:30) so the Tyler host never sees them at once:

```cron
30 7 * * *  /Users/atang/Documents/scar-permits/scripts/tick.sh >> /tmp/sca_tick.cron 2>&1
```

The tick clears the recent year-window cache before re-pulling — otherwise
stale trailing pages could re-introduce old statuses when step 1 parses. It also
holds a run lock (no two ticks at once) and throttles to one scrape per
`--min-interval-hours` (default 6), so a frequent scheduler can't hammer the
Tyler host; pass `--force` to override.

`step2_fetch_details.py` requires a selection filter (`--all`, `--start-year`,
`--since`, `--status`, `--type-like`, or `--limit`) — it won't fetch all 52k
implicitly. Both step 2 scripts are idempotent; the cache makes the backfill
resumable.

Cached search pages: `outputs/raw/sca/permit/<year>/page_NNN.json`. Cached detail:
`outputs/raw/sca/permit_detail/<case_id>.json`. Audit ledgers under
`outputs/step_{0,1,2,3}/`. Database: `outputs/sca_permits.db`.

## Layout

```
src/
  init_db.py                      apply migrations
  step0_fetch_search_results.py   paged JSON search client   (entrypoint)
  step0b_refresh_stale.py         re-pull stale-status old actionable leads (entrypoint)
  step1_parse_search_results.py   JSON -> sca_permits         (entrypoint)
  step2_fetch_details.py          per-record detail GETs      (entrypoint)
  step2_parse_details.py          detail JSON -> detail+contacts (entrypoint)
  step3_score.py                  enriched permits -> sca_leads  (entrypoint)
  step3b_cluster.py               sca_leads -> sca_lead_clusters (entrypoint)
  step4_sync_d1.py                sca_leads -> Cloudflare D1 SQL/push (entrypoint)
  tick.py                         incremental daily refresh orchestrator (entrypoint)
  healthcheck.py                  read-only DB integrity + consistency checks (entrypoint)
  utils/
    config.py    API URLs, headers, FilterModule enum, search-body builder
    auth.py      anonymous headers (+ optional SCA_BEARER_TOKEN fallback)
    io.py        SQLite connect (WAL) + atomic writes + migrations  (from cu-permits)
    normalize.py address canonicalization for clustering            (from cu-permits)
    step_0/fetch.py     year-chunked paged POST + caching + 429 backoff
    step_1/parsing.py   EntityResults[] -> row dicts
    step_2/detail.py    per-record GET + caching + 429 backoff
    step_2/parsing.py   detail JSON -> detail row + contact rows (role normalize)
    step_3/scoring.py        gates×factors model (status buckets, size, banding)
    step_3/type_fit_rules.json  type -> score/category rule table (editable)
    step_3/clustering.py     parcel/address cluster key + project aggregation
scripts/tick.sh                         cron wrapper (venv + logging) around tick.py
tests/test_logic.py                     unittest regression tests for the pure logic
migrations/0001_init_sca_permits.sql    sca_permits table
migrations/0002_add_permit_detail.sql   sca_permit_detail + sca_permit_contacts
migrations/0003_add_lead_scores.sql     sca_leads
migrations/0004_add_scope_and_holds.sql scope (sq ft/stories/construction) + active/blocking holds
migrations/0005_add_lead_clusters.sql   cluster_id on sca_leads + sca_lead_clusters
migrations/0006_add_recency_factor.sql  recency_factor col (decays stale migrated permits)
migrations/0007_add_contact_role.sql    contact_role on sca_leads (which tier the contact came from)
migrations/0008_clusters_contact_role.sql contact_role on sca_lead_clusters (parity w/ per-permit)
docs/handoff/                           design notes (platform, API, scoring)
```

## Notes

- **No proxy, no Selenium, no Cloudflare machinery** — a polite single-IP `requests`
  client is correct here. Step 0 sleeps ~1s between pages.
- Auth is anonymous; `SCA_BEARER_TOKEN` in `.env` is a fallback only (see `.env.example`),
  used if the portal ever stops serving anonymous requests. The scraper never logs in.
- Tables use the `sca_` prefix (per the handoff bundle).

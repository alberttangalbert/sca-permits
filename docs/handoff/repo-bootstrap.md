# Repo bootstrap — `sca-permits`

Concrete starter layout for the new repo. Fork base is **`cu-permits`**
(Cupertino) for *structure*, but steps 0–2 are authored fresh against the
EnerGov JSON API. Copy the skeleton, swap constants to `sca_*`, rewrite the
fetch/parse/detail layer.

## Top-level layout (mirror `cu-permits`)

```
sca-permits/
├── README.md                          # NEW — adapt cu-permits README (EnerGov JSON, sca_*)
├── .env.example                       # COPY (no proxy; D1 creds only, deferred)
├── .gitignore                         # COPY
├── requirements.txt                   # COPY minus selenium/bs4-heavy bits (requests + maybe bs4 for stray HTML)
├── docs/
│   ├── SCHEMA.md                      # NEW — written after first scrape
│   └── handoff/san-carlos/            # COPY this bundle in for provenance
├── migrations/
│   ├── 0001_init_sca_permits.sql      # NEW — sca_permits + detail + workflow + fees + related (+contacts if public)
│   ├── 0002_init_sca_clean.sql        # NEW — placeholder
│   ├── 0003_init_sca_leads.sql        # NEW — placeholder
│   └── 0004_sca_views.sql             # NEW — v_sca_journey, v_sca_top_leads
├── outputs/
│   ├── raw/sca/{permit,plan}/         # gitignored — cached search/detail JSON
│   └── sca_permits.db                 # gitignored
├── logs/
├── scripts/
│   ├── tick.sh                        # COPY — drop the CF cooldown gate; keep the daily-pull + refresh structure
│   ├── status.sh / export-leads.sh    # COPY — adjust table names to sca_*
│   ├── backfill_years.sh              # COPY only IF a result cap is found (see search-api.md)
│   └── README.md                      # COPY + adjust (no CF, EnerGov notes)
└── src/
    ├── init_db.py                     # COPY
    ├── step0_fetch_search_results.py  # REWRITE — JSON search client (paged POST)
    ├── step1_parse_search_results.py  # REWRITE (much smaller) — JSON → sca_permits
    ├── step2_scrape_details.py        # COPY orchestrator; NEW JSON detail backend
    ├── step3_analyze.py               # COPY + re-derive rule tables
    └── utils/
        ├── config.py                  # EDIT — EnerGov base URL, module enum map, JSON-API URL builders
        ├── io.py / normalize.py       # COPY (address_norm/split_address reused for clustering)
        ├── step_0/fetch.py            # REWRITE — JSON search + PageNumber pagination (no VIEWSTATE)
        ├── step_1/parsing.py          # REWRITE — read EntityResults[] JSON (no BeautifulSoup grid parse)
        └── step_2/
            ├── scrape_requests.py     # REWRITE — GET permit?id + sub-resource GETs; map JSON
            ├── extract.py             # EDIT — valuation from JSON field; SF-from-desc only as fallback
            ├── schema.py              # EDIT — DETAIL_COLUMNS: add valuation, contacts(if public); keep workflow/fees/related
            └── state.py               # COPY — load_pending TIER ordering transfers as-is
```

## Files that copy ~verbatim

- `.gitignore`, `src/utils/io.py`, `src/utils/normalize.py`,
  `src/utils/step_2/state.py` (queue/`load_pending` logic is platform-agnostic),
  `src/step2_scrape_details.py` orchestrator shell, `scripts/status.sh` /
  `export-leads.sh` (table-name edits only), `src/step3_analyze.py` (logic;
  rule *tables* get re-derived).

## Files that copy with config-only swaps

- `src/utils/config.py` → EnerGov base URL, module enum map (Permit/Plan ⚠️
  confirm ints), JSON-API URL builders, `sca_*` table names.
- `scripts/tick.sh` → keep daily-pull + rolling-refresh structure; **remove**
  the `MIN_CF_COOLDOWN_SECONDS` gate and `cf_probe.py` preflight (no CF).

## Files that need real authoring

| File | Work |
|---|---|
| `src/utils/step_0/fetch.py` | **Core new code.** Paged JSON search POST; cache each page; year-chunk only if a cap exists. |
| `src/step1_parse_search_results.py` / `step_1/parsing.py` | Read `EntityResults[]` JSON into `sca_permits`; dedup on GUID; filter drafts by status. |
| `src/utils/step_2/scrape_requests.py` | **Core new code.** GET `permit?id=<GUID>` + sub-resource GETs; map JSON → detail/workflow/fees/related/contacts. |
| `src/utils/step_2/extract.py` | Pull valuation/SF from the JSON field; keep the description-SF regex only as a fallback; classify contact roles. |
| `migrations/0001_init_sca_permits.sql` | Author `sca_*` schema; valuation column first-class; `sca_permit_related` and (if public) `sca_permit_contacts`. |
| `src/step3_analyze.py` rule tables | Re-derive `type_fit_rules.json`, `STATUS_BUCKETS`, keyword lists from real SC vocabulary. |
| `docs/SCHEMA.md` | After first end-to-end run. |

## `.env.example`

```dotenv
# Cloudflare D1 (step 4, deferred)
CF_ACCOUNT_ID=
CF_D1_DATABASE_ID=        # new San Carlos D1, or shared `permits` D1 with sca_* tables
CF_API_TOKEN=

# NOTE: no proxy. San Carlos is a public JSON API on tylerhost.net with no
# Cloudflare gate; a polite single-IP direct scrape is correct. Do NOT set a proxy.
# (And no CF cooldown vars — that machinery is Accela-only.)
```

## Day-1 smoke test

```bash
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
python3 src/init_db.py

# step 0 + 1: prove the JSON search + parse on a tiny window
python3 src/step0_fetch_search_results.py --module Permit --lookback-days 30
python3 src/step1_parse_search_results.py --module Permit

# step 2: prove the detail GET on a few records
python3 src/step2_scrape_details.py --module Permit --limit 3

sqlite3 outputs/sca_permits.db \
  "SELECT record_number, record_type_detail, record_status_detail,
          valuation, workflow_completion_pct FROM sca_permits_detail LIMIT 5"
```

If step 2 returns populated valuation + workflow for a few records, the JSON
backend works and the rest of the pipeline follows the cu-permits playbook.

## Decisions before day 1

1. **History depth** — does CSS return pre-June-2025 permits? If not, decide
   whether the legacy system is in scope (see [`known-issues.md`](./known-issues.md)).
2. **Modules** — Permit + Plan confirmed in scope; add others (Code Cases,
   Licenses) only if they expand the residential funnel.
3. **Size signal** — confirm the EnerGov valuation/SF field exists; if not,
   fall back to description SF.
4. **Contacts** — confirm public visibility; if public, model
   `sca_permit_contacts` first-class.
5. **D1** — new database vs shared `permits` D1 with `sca_*` tables.
6. **Cron stagger** — offset from Fremont 4:30 / SC 5:30 / Cupertino 6:30, e.g.
   7:30 AM Pacific.

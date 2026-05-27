# Known issues, risks, and open questions

## Resolved during recon (2026-05-26, read-only)

- ✅ **Platform is Tyler EnerGov Citizen Self Service (CSS)** at
  `https://sancarlosca-energovweb.tylerhost.net/apps/selfservice` — an AngularJS
  SPA over a JSON REST API. **Not Accela**, so the Accela scraping mechanics
  don't transfer; the pipeline architecture and scoring model do.
- ✅ **CSS went live June 3, 2025** (per the city's permits page).
- ✅ **Sausalito red herring cleared** — the `saus-trk.aspgov.com` eTRAKiT
  instance that surfaces in searches is the **City of Sausalito**, not San
  Carlos. Don't aim the scraper at it.
- ✅ **No Cloudflare gate observed** on the Tyler host — the cu-permits CF
  cooldown machinery is expected to be unnecessary.

## Open unknowns (validate during recon / v1)

### 1. History depth — the cutover gap (HIGHEST RISK)

CSS launched **June 3, 2025**. The entire value of `cu-permits` was backfilling
*years* of history to surface leads; if San Carlos's CSS only returns
post-cutover records, a historical backfill is **impossible from CSS alone**.

- **❓ Recon task:** run a search with a date window starting well before June
  2025 (e.g. `ApplyDateFrom = 2022-01-01`) and see whether any records come
  back. If the oldest result is ~June 2025, CSS holds **only post-cutover**
  data.
- **If only post-cutover:** the corpus is small (≤ ~1 year at the time of
  writing) but *every record is fresh* — arguably *better* lead quality than an
  old archive. Set expectations with the user accordingly.
- **If history is needed:** identify San Carlos's **legacy permitting system**
  (the pre-June-2025 portal — possibly Accela, eTRAKiT, or a county system) and
  decide whether scraping it is in scope. That's a separate recon + possibly a
  separate scraper. **Surface this to the user before promising a backfill.**

### 2. Is contact data public?

EnerGov has a contacts sub-resource, but agencies toggle public visibility.

- **❓ Recon task:** open a permit's Contacts tab as an anonymous user; check
  whether owner/applicant/contractor names (and contractor license #) are
  returned in the JSON.
- **If public:** San Carlos is a *richer* lead source than any Accela city
  (Cupertino exposed no public contacts). Model `sca_permit_contacts`
  first-class and classify roles (carry cu-permits bug #16: "Agent for Owner"
  ≠ OWNER).
- **If hidden:** fall back to address→county-assessor cross-reference for
  self-filers, same as Cupertino. EnerGov likely exposes a structured parcel/APN
  field, making this easier than Cupertino (where APN was AJAX-gated).

### 3. Result cap / paging limit

Accela capped at ~10k per query (cu-permits memory `cu-permits-accela-10k-cap`),
fixed by year-chunking. EnerGov has its own limits.

- **❓ Recon task:** run a deliberately wide search and watch whether
  `TotalRecords`/`TotalPages` keeps climbing or **clamps**, and whether paging
  past some `PageNumber` returns empty despite a higher `TotalPages`.
- **If a cap exists:** reuse the cu-permits year-chunking remedy (port
  `backfill_years.sh`, swap in the JSON search). Given the small post-June-2025
  corpus, a cap may never bite — but confirm, don't assume.

### 4. Auth / anti-forgery on the API

- **❓ Recon task:** confirm the public search + detail GETs work **anonymously**
  with plain `requests`. Watch for a required `RequestVerificationToken` header,
  an anti-forgery cookie, or a bootstrap token the SPA fetches first. If the
  search 403s without it, replicate the token-fetch the SPA does on load.

### 5. Exact API contract is unverified

Everything concrete in `search-api.md` / `detail-api.md` (URLs, body keys, enum
ints, field names) is **⚠️ VERIFY** — derived from the EnerGov CSS *platform
shape*, not from probing this instance. The 30-minute DevTools recon in
[`platform-identification.md`](./platform-identification.md) removes this risk
entirely. Do it before writing the fetch code.

### 6. Module enum & naming

- **❓ Recon task:** confirm the `SearchModule` integer for Permit (and Plan),
  the per-module criteria object key names, and which modules San Carlos has
  enabled (Permit, Plan, Inspection, Code Case, License, …). The settings/
  bootstrap GET the SPA fires on load enumerates these.

## Anti-bot signals to respect

- Public records, public API — but still be polite: real User-Agent, modest
  delays (0.5–1 s), single IP, **no writes, no auth bypass, no login-gated
  endpoints**. Only call what the anonymous SPA itself calls.
- Handle **HTTP 429** with exponential backoff if it appears (the EnerGov analog
  of Accela's 1015) — but don't build the full CF cooldown gate preemptively.

## Data-volume risk

San Carlos is a **small city** and CSS is **post-June-2025 only** — the corpus
is tiny (likely a few thousand records). Well under any D1 limit even combined
with the other cities. Not a concern; if anything, the risk is *too little*
history, not too much (see #1).

## Audit-bug fixes to carry over from cu-permits

Build clean from day one — see the table in
[`record-types-and-scoring.md`](./record-types-and-scoring.md)
(#11/#14 cluster key, #16 Agent-for-Owner, #17 size factor, #19 staleness,
#20 pending payment, #21 leads filter, #22 applicant volume).

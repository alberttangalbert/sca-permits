# Detail API — EnerGov CSS record detail (step 2 source)

The San Carlos analog of Cupertino's `detail-page.md`. The good news up front:
**there is no AJAX-shell problem and no PageMethod replay.** Where Cupertino's
detail page was a near-empty HTML shell you had to re-hydrate with session-bound
PageMethod POSTs, EnerGov serves the record as **JSON from REST routes keyed on
the record GUID** — stateless GETs, no cookie continuity required.

> **Recon status:** the route paths below are **✅ CONFIRMED** present in the
> portal's `app/energov` JS bundle (2026-05-26). Exact query params and response
> field names are **⚠️ VERIFY** — capture from DevTools (we did not exercise them
> with a real GUID).

## Confirmed route map (all under `…/apps/selfservice/api/energov/`)

These are the real routes the SPA calls — read straight from its bundle:

| Purpose | Route | cu-permits column family |
|---|---|---|
| **Global search** | `POST search/search` | step 0 — see [`search-api.md`](./search-api.md) |
| **Permit detail** | `permits/permit/`, `permits/permitdetail` | core `record_*`, dates, valuation, parcel |
| **Plan detail** | `plans/` | core (Plan module) |
| **Workflow** | `workflow/summary/` | `workflow_*` (stages/completion/current/last-event) |
| **Reviews / submittals** | `entity/submittals`, `entity/submittals/itemreviews/search/` | review steps |
| **Fees** | `entity/fees/search` | fees table |
| **Inspections** | `entity/inspections/search/search`, `entity/previousInspections/search` | inspections |
| **Contacts** | `entity/contacts/search/search` | **owner / applicant / contractor** — see below |
| **Conditions** | `entity/conditions` | conditions |
| **Holds** | `entity/holds/` | holds (a near-issuance blocker signal) |
| **Notes** | `entity/notes/search` | optional |
| **Violations** | `entity/violations/search` | (Code Case module) |
| **Checklist** | `entity/checklist/search` | optional |
| **Summary** | `entity/summary/` | a rolled-up record summary — try this first |
| **Attachments** | `entity/attachments/search/search` | usually skip |

The `entity/*` routes are **generic sub-resource endpoints** keyed on the record
id + module — the EnerGov equivalent of Cupertino's per-section PageMethods, but
RESTful and stateless. Open each sub-tab in DevTools to capture its exact query
params (typically the record GUID + a module/case-type discriminator).

## The core call (⚠️ VERIFY params)

From a search result you have the record **GUID `Id`**. Fetch the record via
`permits/permit/` (or `permits/permitdetail`), passing the id — confirm whether
it's a path segment (`permits/permit/<GUID>`) or a query param. No session
seeding needed; the id is explicit, so detail fetches are **parallel-safe**
(unlike Accela's mandatory one-session-per-record).

## Contact data — ✅ the endpoint exists (a big win over Cupertino)

`entity/contacts/search/search` is a **confirmed route**. Cupertino exposed
**no** public contacts at all, forcing an address→assessor cross-reference.
EnerGov has a real contacts sub-resource — so San Carlos *can* surface
owner/applicant/contractor names directly.

- ❓ **One recon check:** call it anonymously for a real permit and confirm the
  agency leaves contact names publicly visible (some hide them for anonymous
  users). If visible → San Carlos is the richest lead source of any city.
- Whatever you get, classify roles (Owner / Applicant / Contractor / Agent) and
  carry cu-permits **bug #16: "Agent for Owner" must not classify as OWNER**.

## Fields EnerGov typically exposes (and how they beat Cupertino)

- ⚠️-likely **Structured valuation / estimated project cost / square footage.**
  EnerGov permits usually carry a numeric **valuation**/**SF** field — the
  **size signal**. Cupertino had *no* Job Value and had to regex `(\d[\d,]*)\s*SF`
  from the description. Confirm the field on `permits/permit` and wire it to the
  size factor; keep the description-SF regex only as a fallback.
- **Workflow / reviews** as structured rows (step, status, dates, reviewer) from
  `workflow/summary/` + `entity/submittals/itemreviews` — derive the same
  `workflow_stages_total/complete/completion_pct/current_stage/last_event_*`
  columns cu-permits uses, but from JSON, not `Marked as …` regex.
- **Parcel / APN** — usually a structured field on the core record (Cupertino's
  was AJAX-gated and effectively unobtainable), making the assessor cross-ref
  easier if you still need it.
- **Holds** (`entity/holds/`) — an open hold is a near-issuance blocker; useful
  as a status nuance.

## Related records / project journey

❓ Look for an "associated/related cases" field on the core detail or a dedicated
route (none jumped out of the bundle beyond `entity/summary/`). If present,
persist it first-class into `sca_permit_related` (like cu-permits'
`cup_permit_related`) and use it as the primary journey source. If absent, fall
back to **address clustering** with cu-permits' `address_norm` (keep the unit in
a separate column so multifamily doesn't over-collapse; treat civic/corporate
addresses as noisy).

## Cost per record & pacing

A fully-enriched record is **1 core GET + a few sub-resource GETs** (~3–6
requests), all stateless JSON, **no CF gate** (IIS host). Throughput should far
exceed Cupertino's CF-throttled ~1,400/hr — but stay polite
([`scraping-strategy.md`](./scraping-strategy.md)). Consider the tiered approach:
cheap core+workflow for every record, heavier sub-resources (fees, contacts)
only for pre-scored HIGH/MEDIUM leads — same budget discipline cu-permits used.

## Auth note

The portal loads `oidc-client.min.js` — **OpenID Connect** is used for
*logged-in* features. Public search + public detail are intended to work
**anonymously**; confirm none of the routes you need require a bearer token.
(If the search POST needs an anti-forgery token, the detail GETs may too —
capture headers once in DevTools and reuse the pattern.)

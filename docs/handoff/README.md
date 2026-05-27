# San Carlos permit-intel — design notes for a separate repo

You're building a sibling to **`cu-permits`** (Cupertino) for the **City of
San Carlos**. Same decision as every prior city fork: stand up a fresh repo
(`sca-permits`) rather than parameterize the existing code. This doc bundle
captures everything learned about San Carlos's portal so the new build doesn't
repeat the discovery work.

Lineage so far: **Fremont → Santa Clara → Cupertino**, all on the **Accela**
ACA platform. **San Carlos breaks the lineage** — it is **not Accela**. So
these docs are written as a **two-part split**:

- **What transfers from `cu-permits`** — the *architecture* (4-step pipeline,
  two-tier data model, gates×factors lead scoring, the lead philosophy, polite
  scraping discipline). This carries over almost verbatim; it's city- and
  platform-agnostic.
- **What is brand-new** — the *entire scraping layer*. San Carlos runs a
  completely different permitting system with a completely different access
  model. Everything in step 0 / step 1 / step 2 (how you fetch and parse) is a
  rewrite. Step 3 (scoring) and the data model mostly survive.

---

## TL;DR — the load-bearing fact

> **San Carlos runs Tyler EnerGov "Citizen Self Service" (CSS), NOT Accela.**

Recon (read-only, **2026-05-26**, against the live public portal):

- Portal: **`https://sancarlosca-energovweb.tylerhost.net/apps/selfservice`**
- Platform/vendor: **Tyler Technologies — EnerGov Citizen Self Service (CSS)**
  (newer Tyler installs rebrand this "Civic Access" / "Enterprise Permitting &
  Licensing" — same product family). Page title is **"SelfService Public Site"**.
- It is an **AngularJS single-page app** backed by a **JSON REST API**, *not*
  an ASP.NET WebForms site. There are **no `__doPostBack` postbacks, no
  `__VIEWSTATE`, no PageMethods** — the things every Accela doc in
  `cu-permits/docs/handoff/cupertino/` describes **do not apply here**.
- **Go-live: June 3, 2025.** The city's permits page states it adopted "a new
  application process with a new permitting software" as of that date. This is
  the single biggest data-completeness risk — see "Open questions" below and
  [`known-issues.md`](./known-issues.md).
- Hosted on `tylerhost.net` (Tyler's SaaS). **Not fronted by Cloudflare** the
  way Accela's `aca-prod.accela.com` is — so the entire Cupertino "CF cooldown
  gate / 1015 flicker / per-IP rate-limit" machinery is **probably unnecessary**
  (confirm during recon).

### Why this is *good* news for the scrape

A JSON API is far friendlier than Accela's WebForms + AJAX-PageMethod maze:

- Step 0 is a **JSON POST search** returning structured records — no HTML
  grid parsing, no ViewState round-trips, no pagination postbacks.
- Step 1 ("parse") collapses to **reading JSON fields** — BeautifulSoup mostly
  disappears.
- Step 2 detail is a **`GET …/permit?id=<GUID>` JSON call** — no per-record
  cookie-seeded session, no PageMethod replay, no AJAX shell problem.
- EnerGov usually exposes **structured valuation / estimated-cost / square-
  footage fields** — so San Carlos likely has a real **size signal**, unlike
  Cupertino (which had to regex SF out of the description).

### Why it's still real work

- The **exact API paths, request bodies, enum values, and field names are
  instance-specific** and must be captured from the browser **DevTools Network
  tab** against this portal. This bundle gives you the *shape* of the EnerGov
  CSS API and a recon checklist — it does **not** hand you verified endpoints
  (we did read-only page recon, not API probing). Treat every concrete path or
  param below as **⚠️ VERIFY** until you've watched it fire in DevTools.
- The **June 2025 cutover** means historical permits may live in a legacy
  system and may not be searchable in CSS. The whole point of `cu-permits` was
  backfilling history; clarify how far back CSS goes before promising it.

Effort estimate for a fresh `sca-permits`: **2–4 dev-days** for an MVP. The
scraping layer is a clean rewrite but *simpler* than either Accela city (no
Selenium, no CF gate, no PageMethod replay); the savings roughly offset the
"new platform, zero reused scraper code" cost. Scoring/data-model reuse is high.

---

## File index

| File | What it covers |
|---|---|
| `README.md` (this file) | TL;DR + what transfers vs. what's new + the EnerGov pivot |
| [`platform-identification.md`](./platform-identification.md) | **How we know it's EnerGov CSS; how to extend the recon (DevTools method)** |
| [`search-api.md`](./search-api.md) | The CSS Global Search JSON API → step 0/1 source. Recon checklist + result-cap question |
| [`detail-api.md`](./detail-api.md) | Record-detail JSON API (permit/plan by GUID) → step 2 source. Sub-resources, contacts, workflow |
| [`record-types-and-scoring.md`](./record-types-and-scoring.md) | The gates×factors model (carried over) + a recon checklist to harvest SC's real types/statuses |
| [`scraping-strategy.md`](./scraping-strategy.md) | requests+JSON strategy, anti-bot posture (no CF expected), pacing, ethics |
| [`pipeline-deltas.md`](./pipeline-deltas.md) | Step-by-step diff: `cu-permits` (Accela HTML) → `sca-permits` (EnerGov JSON) |
| [`repo-bootstrap.md`](./repo-bootstrap.md) | Concrete starter layout for `sca-permits`, table prefix, smoke test |
| [`known-issues.md`](./known-issues.md) | Risks, the June-2025 legacy-history gap, contact-data + result-cap unknowns |

---

## What we'd actually do

1. **Recon the API first** (half a day) — open the portal, do a public search,
   and watch the DevTools Network tab. Capture the search POST URL + body +
   response, and one detail GET. See [`platform-identification.md`](./platform-identification.md)
   and [`search-api.md`](./search-api.md). Everything downstream depends on this.
2. **Bootstrap the repo** (`sca-permits`) — copy `cu-permits` *structure*, swap
   constants, `sca_*` table prefix. See [`repo-bootstrap.md`](./repo-bootstrap.md).
3. **Step 0 — search.** New driver: paged JSON POST to the CSS search endpoint,
   cache each page's JSON to `htmls/sca/{module}/` (or `outputs/raw/`). No
   Selenium, no postbacks.
4. **Step 1 — parse.** Read JSON fields into `sca_permits` (dedup on the record
   GUID/number). Much smaller than the Accela HTML parser.
5. **Step 2 — detail.** `GET …/permit?id=<GUID>` per record; persist workflow /
   fees / inspections / contacts / valuation from the JSON. No CF gate expected.
6. **Step 3 — analyze + score.** Architecture (gates × factors) unchanged.
   Re-derive `type_fit_rules.json`, `STATUS_BUCKETS`, keyword lists from San
   Carlos's vocabulary. Use EnerGov's structured valuation as the size signal.
7. **Step 4 — sync to D1** (deferred, as in every city). `sca_*` table prefix.

---

## Open questions for the user

1. **History depth (critical).** CSS went live **June 3, 2025**. Does the CSS
   public search return permits filed *before* the cutover (migrated), or only
   post-June-2025 records? If only post-cutover, a historical backfill (the
   raison d'être of `cu-permits`) needs the **legacy system** — identify it
   (the prior San Carlos portal) and decide whether it's in scope. See
   [`known-issues.md`](./known-issues.md).
2. **Contact data.** Does the CSS detail JSON expose owner/applicant/contractor
   names? EnerGov *can* (it has a contacts sub-resource), but agencies toggle
   public visibility. If present, San Carlos is *richer* than Cupertino (which
   had no public contacts at all). Confirm in recon.
3. **Module scope.** The portal lists Permits, Plans, Inspections, Code Cases,
   Licenses, etc. Recommend **Permits + Plans** for the residential lead funnel
   (mirrors Cupertino's Building + Planning). Confirm.
4. **Result cap.** Accela capped at ~10k per query (the lesson baked into
   `cu-permits`); EnerGov CSS has its own paging/total limits. Determine the cap
   and whether year-chunking is needed for any backfill. See [`search-api.md`](./search-api.md).
5. **D1 / frontend.** New D1 vs shared `permits` D1 with `sca_*` tables — same
   open question as every prior city.

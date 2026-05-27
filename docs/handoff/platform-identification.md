# Platform identification — how we know it's EnerGov CSS

The first and most important recon question for any new city is **"what
permitting platform is this?"** — it determines whether *any* of the prior
Accela work transfers. For San Carlos the answer is **no, it's a different
platform**, so this doc records how that was established and how to finish the
recon the rest of the bundle depends on.

## Recon status legend (used throughout this bundle)

| Marker | Meaning |
|---|---|
| ✅ **CONFIRMED** | Verified by read-only recon on 2026-05-26 (page fetch / search results) |
| ⚠️ **VERIFY** | Stated from general EnerGov CSS knowledge / the platform's typical shape — **must be confirmed in DevTools against this instance** before you code against it |
| ❓ **UNKNOWN** | Not yet investigated; an open recon task |

Everything in `search-api.md` / `detail-api.md` that names a concrete URL,
param, or field is **⚠️ VERIFY** unless explicitly marked ✅. We did *page*
recon, not *API* recon — we did not probe the JSON endpoints.

## What was confirmed (2026-05-26, read-only)

- ✅ The City of San Carlos building/permits page links to a self-service
  portal and states: *"As of June 3, 2025, the City of San Carlos is now
  implementing a new application process with a new permitting software for all
  permits and projects."*
- ✅ That portal is **`https://sancarlosca-energovweb.tylerhost.net/apps/selfservice`**.
- ✅ It is a **Tyler EnerGov Citizen Self Service (CSS)** install:
  - hostname `*-energovweb.tylerhost.net` is Tyler's EnerGov SaaS naming;
  - the path `/apps/selfservice` is the EnerGov CSS AngularJS app;
  - the page title is **"SelfService Public Site"**;
  - the HTML is an **AngularJS SPA** (`{{ }}` bindings, `vm.` view-model
    references, `/apps/selfservice/Content/app/...` asset paths) — no
    server-rendered grid, no `__VIEWSTATE`, no `__doPostBack`.
- ✅ It is **not Accela** and **not eTRAKiT**. (During recon, a Bay-Area
  eTRAKiT instance `saus-trk.aspgov.com` turned up in search — that is the
  **City of Sausalito**, a red herring. Don't be misled by it.)
- ✅ Modules visible in the CSS chrome include account/cart/login plumbing;
  the public record types (Permits, Plans, Inspections, Code Cases, Licenses)
  are the standard EnerGov CSS module set — ⚠️ confirm exactly which are
  enabled for San Carlos in recon.

### API-level facts confirmed (2026-05-26 live probing)

Probed the API directly (read the portal's `app/energov` JS bundle + endpoint
HTTP probes). These were ⚠️ VERIFY in the first draft and are now ✅ CONFIRMED:

- ✅ **API base:** `https://sancarlosca-energovweb.tylerhost.net/apps/selfservice/api/energov/`
  — a plain **ASP.NET Web API** (IIS/10.0, **no Cloudflare**). Route-miss →
  **404** `{"Message":"No HTTP resource was found…"}`; real route + bad body →
  **500** `{"Message":"An error has occurred."}` (opaque — **no param hints**,
  unlike Accela's PageMethods).
- ✅ **Search endpoint:** `POST …/search/search` (confirmed real route).
- ✅ **`SearchModule` enum:** All=1, **Permit=2, Plan=3**, Inspection=4,
  CodeCase=5, Request=6, Business=7, BusinessLicense=8.
- ✅ **`PermitCriteria` fields** (and sibling criteria) — see
  [`search-api.md`](./search-api.md). Type/status filters use a `"none"`
  sentinel, not null.
- ✅ **Detail + sub-resource routes** — `permits/permit/`, `plans/`,
  `workflow/summary/`, `entity/{fees,inspections,contacts,conditions,holds,
  submittals,…}/…` — see [`detail-api.md`](./detail-api.md). **A contacts
  endpoint exists** (Cupertino had none).
- ✅ Auth via **OIDC** (`oidc-client.min.js`) for logged-in features; public
  search/detail intended anonymous.
- ⚠️ **Only remaining gap:** the exact working **search request envelope** —
  two reconstructed bodies returned the opaque 500, so it needs one detail
  (likely an anti-forgery token / bootstrapped session) that requires a browser
  DevTools capture. Scoped at the bottom of [`search-api.md`](./search-api.md).

## What EnerGov CSS *is* (orientation for the rewrite)

EnerGov CSS is Tyler's public citizen portal in front of the EnerGov /
Enterprise Permitting & Licensing back office. Mental model:

- **Front end:** an AngularJS SPA (older installs, `/apps/selfservice`) or a
  newer "Civic Access" Angular build. San Carlos is the **older AngularJS CSS**
  (`/apps/selfservice#/…` fragment routing).
- **Back end:** a **JSON REST API** on the same host under
  `…/apps/selfservice/api/energov/…` (⚠️ VERIFY exact base). The SPA calls it;
  so do you. **This API is the scrape target — not the HTML.**
- **Auth:** public search and public record detail are reachable **anonymously**
  (no login) ⚠️ VERIFY. Some endpoints may require an anti-forgery / request-
  verification token or a specific `Content-Type`; capture whatever headers the
  browser sends.

Contrast with the Accela cities (so you know what *not* to carry over):

| Concept (Accela / Cupertino) | EnerGov CSS (San Carlos) |
|---|---|
| `CapHome.aspx` WebForms search + `__doPostBack` pagination | **JSON POST** to a search endpoint, `PageNumber`/`PageSize` in the body |
| `__VIEWSTATE` / `__EVENTVALIDATION` round-trips | **None** — stateless JSON requests |
| `CapDetail.aspx` + AJAX **PageMethods** (`GetProcessingData`, `GetBuildCapTree`) | **`GET …/permit?id=<GUID>`** returns the whole record (+ sub-resource GETs) |
| capID1/capID2/capID3 composite key from the search row | a **record GUID** (and a human permit number) from the search result |
| Cloudflare 1015 flicker, per-IP rate-limit, cooldown gate | Tyler host, **no Cloudflare expected** — likely just be polite ⚠️ VERIFY |
| Selenium fallback for ViewState tabs | **Never needed** — it's all JSON |
| Cookie continuity mandatory (session-held record context) | **Stateless** — pass the record id explicitly each call |

## How to finish the recon (DevTools method — do this first)

Everything in `search-api.md` and `detail-api.md` should be re-derived live; it
takes ~30 minutes and removes all the ⚠️ VERIFY uncertainty:

1. Open `https://sancarlosca-energovweb.tylerhost.net/apps/selfservice#/home`
   in Chrome. Open **DevTools → Network**, filter to **Fetch/XHR**.
2. Click **Search** (Global Search / "Search Public Records"). Choose the
   **Permit** module, set a small date window, and run the search.
3. In Network, find the **search POST** (look for a request to `…/search…`).
   Capture, via **right-click → Copy → Copy as cURL**:
   - the full **request URL**,
   - the **request body JSON** (this reveals the criteria object shape,
     `SearchModule` enum value, date field names, `PageNumber`/`PageSize`),
   - the **response JSON** (reveals `EntityResults[]` field names, the total-
     count / total-pages fields, and the per-record **id/GUID**).
   - the **request headers** (Content-Type, any `Authorization`,
     `RequestVerificationToken`, `X-…` custom headers, cookies).
4. Click into a single result to open its detail. Capture the **detail GET(s)**
   — usually `…/permit?id=<GUID>` plus sub-resource calls fired as you open
   the Fees / Reviews / Inspections / Contacts sub-tabs.
5. Page to result 2, 3 to confirm how pagination is expressed and whether a
   **total-results cap** kicks in (see [`search-api.md`](./search-api.md)).

Paste the captured cURLs into `search-api.md` / `detail-api.md` to replace the
⚠️ VERIFY placeholders with this instance's truth. After that, the scraper is
a thin client over those two calls.

## Don't reuse from the Accela bundle

For the avoidance of doubt, **none** of these Cupertino artifacts apply to San
Carlos and should not be copied: `__VIEWSTATE` postback recipe, `overflow_marker`,
the PageMethod replay (`GetProcessingData`/`GetBuildCapTree`/`DisplayFee*`), the
capID URL construction, the CF cooldown gate / `cf_probe.py`, the `is_tmp_draft`
`^\d{2}TMP-` regex, the Selenium step-0 fallback. The *pipeline shape* and the
*scoring model* transfer; the *Accela mechanics* do not.

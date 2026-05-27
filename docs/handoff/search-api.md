# Search API — EnerGov CSS Global Search (step 0 / step 1 source)

This is the San Carlos analog of Cupertino's `search-form.md`, but there is **no
HTML form and no postback**. The CSS public search is a **JSON POST to a REST
endpoint** that returns structured records. Step 0 calls it and caches the JSON;
step 1 reads fields out of that JSON. BeautifulSoup is not involved.

> **Recon status:** the endpoint, the API base, the `SearchModule` enum, and the
> `PermitCriteria` field names below are **✅ CONFIRMED** by live probing on
> 2026-05-26 (read from the portal's own `app/energov` JS bundle + endpoint
> probes). The **exact working request body is the one remaining ⚠️ gap** — see
> "The one thing left to capture" at the bottom.

## The endpoint — ✅ CONFIRMED

```
POST https://sancarlosca-energovweb.tylerhost.net/apps/selfservice/api/energov/search/search
Content-Type: application/json
X-Requested-With: XMLHttpRequest
Accept: application/json
Referer: https://sancarlosca-energovweb.tylerhost.net/apps/selfservice
```

- ✅ The **API base** is `…/apps/selfservice/api/energov/`. It is a plain
  **ASP.NET Web API** returning JSON. A wrong route returns **HTTP 404** with
  `{"Message":"No HTTP resource was found that matches the request URI '…'"}`;
  a real route with a bad body returns **HTTP 500** with the opaque
  `{"Message":"An error has occurred."}`. Use the 404-vs-500 distinction to
  confirm a route exists.
- ✅ `search/search` is a **real route** (returns 500 on a malformed body, not
  404). The host is **IIS/10.0, no Cloudflare** — so none of the cu-permits CF
  machinery is needed (see [`scraping-strategy.md`](./scraping-strategy.md)).
- ⚠️ **Important:** unlike Accela's PageMethods (which name the missing param in
  the 500 body), EnerGov returns a **generic, hint-free 500**. You cannot
  reverse the body by trial-and-error from error messages — capture the real
  request from the browser instead (see bottom).

## The `SearchModule` enum — ✅ CONFIRMED

Read directly from the portal's `app/energov` bundle:

| Value | Module |
|---|---|
| 1 | All |
| **2** | **Permit** |
| **3** | **Plan** |
| 4 | Inspection |
| 5 | CodeCase |
| 6 | Request |
| 7 | Business |
| 8 | BusinessLicense |

For the residential lead funnel you want **`SearchModule: 2` (Permit)** and
**`3` (Plan)**.

## The request body — field names ✅ CONFIRMED, exact envelope ⚠️ VERIFY

The top-level model carries: `Keyword`, `ExactMatch`, `SearchModule`,
`FilterModule`, `SearchMainAddress`, a **per-module criteria object** for each
module, `TabResultType`, `PageNumber`, `PageSize`, `SortBy`, `SortAscending`.

The **`PermitCriteria`** object's fields (✅ confirmed from the JS):

```jsonc
"PermitCriteria": {
  "PermitNumber": null,
  "PermitTypeId": "none",      // sentinel string "none" when not filtering (NOT null)
  "PermitStatusId": "none",    // sentinel string "none"
  "ProjectName": null,
  "Address": null,
  "ParcelNumber": null,
  "ContactName": null,
  "Description": null,
  "ApplyDateFrom": "2025-06-01T00:00:00",   // ISO datetime; the opened/applied-date window
  "ApplyDateTo":   "2026-05-26T00:00:00",
  "IssueDateFrom": null, "IssueDateTo": null,
  "ExpireDateFrom": null, "ExpireDateTo": null,
  "FinalDateFrom": null, "FinalDateTo": null,
  "SortBy": null,
  "PageNumber": 1,
  "PageSize": 10               // must be a value from the server's pageSizeList (see below)
}
```

The sibling criteria objects use the same shape with their own `*TypeId` /
`*StatusId` (also defaulting to the `"none"` sentinel): **`PlanCriteria`**
(`PlanNumber`, `PlanTypeId`, `PlanStatusId`), `InspectionCriteria`
(`InspectionTypeId`, `InspectionStatusId`), `CodeCaseCriteria`
(`CodeCaseTypeId`, `CodeCaseStatusId`), `RequestCriteria`, `BusinessLicenseCriteria`,
`ProfessionalLicenseCriteria` (`LicenseTypeId`, `LicenseStatusId`),
`ProjectCriteria`.

⚠️ Two reconstructed bodies (Permit-only, and all-modules with ISO dates) both
returned the opaque 500, so the envelope needs one detail we couldn't infer
headless — **most likely an anti-forgery/`RequestVerificationToken` header or a
session the SPA bootstraps on load**, or an exact criteria field the model
requires. Don't burn time guessing — capture it (bottom).

### The doctrine still holds: filter downstream, not in the query

`PermitTypeId`/`PermitStatusId` *could* filter server-side, but keep the
cu-permits rule: **pull everything in the date window (`*TypeId="none"`) and
filter/score downstream**, so a scoring-rule change never forces a re-scrape.

## The response — ⚠️ VERIFY (capture the real shape)

EnerGov search responses wrap results in a `Result` envelope with an
`EntityResults[]` array; each result carries a **record GUID `Id`** (the key
step 2's detail GET needs — the EnerGov analog of Accela's capID), a human case
number, type, status, apply date, and address. Confirm the exact field names
from the captured response — they drive step 1's field mapping.

## Pagination & page size

- ✅ `PageSize` must be one of the server's configured **`pageSizeList`** values
  (the SPA reads `pageSizeList[0].Value` as its default). Capture the allowed
  values; use the largest for a backfill (fewer requests).
- Page by incrementing `PageNumber` until results run out / `TotalPages` is hit.
  No postback, no ViewState.

## The date window & incremental tick

- The window filters on **`ApplyDateFrom/To`** (the applied/opened date). As in
  every prior city, a "last-N-days" tick on this field catches **newly-filed**
  permits only — it won't resurface an older permit that merely changed status.
  So keep the cu-permits **`--refresh-older-than-hours`** rolling re-scrape over
  not-yet-final records to catch workflow/status progression.

## The result-cap question (the EnerGov analog of Accela's 10k lesson)

`cu-permits` learned Accela caps a single search at ~10k and fixed it by
**chunking step 0 by year** (memory `cu-permits-accela-10k-cap`,
`scripts/backfill_years.sh`). EnerGov has its own paging/total limits — **don't
assume they match and don't assume there's none**:

- ❓ Once search works, run a wide window and check whether the total clamps at a
  round number or paging past some `PageNumber` returns empty.
- If a cap exists, reuse the year-chunking remedy (port `backfill_years.sh`,
  swap the Accela CLI for this JSON POST; step 1 dedups on the GUID so
  overlapping windows are free).
- But note: San Carlos CSS is **post-June-2025 only** (see
  [`known-issues.md`](./known-issues.md)) and it's a small city, so the corpus
  may be small enough that a cap never bites. Confirm both first.

## What step 0 caches

Write each page's raw response JSON to `outputs/raw/sca/{module}/`. Caching the
raw JSON makes step 1 re-parses free and gives an audit trail — same discipline
as the Accela cities caching result-grid HTML.

## The one thing left to capture (30 seconds in DevTools)

Everything above is confirmed except the **exact working request envelope**.
To finish it:

1. Open the portal, DevTools → Network → Fetch/XHR.
2. Run a Permit search with a date range.
3. Find the `search/search` POST → **right-click → Copy → Copy as cURL**.
4. Diff its body against the model above (note any extra top-level field) and
   copy its **request headers** (especially any `RequestVerificationToken` /
   anti-forgery cookie). Paste the working cURL here.

After that, step 0 is a thin client over this one call.

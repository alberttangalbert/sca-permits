# Record types & lead scoring (step 3)

This is the part that **transfers almost entirely** from `cu-permits`. The lead
philosophy and the scoring architecture are platform-agnostic — only the
*vocabulary* (the actual record-type strings, statuses, and workflow stage names)
is San Carlos-specific and must be harvested after the first scrape.

## The lead philosophy (carried over verbatim)

We're building a **residential-construction lead funnel for a GC**. The bullseye
is the homeowner who has **city approval (or near it) but hasn't engaged a
contractor yet** — new homes, ADUs, additions, and substantial remodels.
Sub-trades (solar, re-roof, HVAC, water heater, plumbing, electrical), permits,
and admin/noise records score near zero. This is identical across Fremont, Santa
Clara, and Cupertino — keep it.

## The scoring architecture (carried over)

`cu-permits` step 3 scores each record as **gates × factors** (a multiplicative
model), derived in `src/step3_analyze.py` + rule tables, and bands the result
into **HIGH / MEDIUM / LOW / DROP**. The pieces, all of which survive the
platform change:

- **`type_fit`** — a 0–1 score per record type (new SFR / ADU = 1.0; addition
  = 0.9; alteration/remodel = 0.75; multifamily alteration = 0.6; Planning
  pre-permits 0.5–0.85; sub-trades 0.1–0.15; tree/sign/event ≈ 0). Driven by a
  `type_fit_rules.json` table, first-to-last substring match on the record type
  (+ optional description keyword to split a broad type like "Residential New"
  into ADU vs new-SFR).
- **size factor** — bigger project = better lead. In Cupertino this was the
  summed square footage parsed from the description (no Job Value existed). **In
  San Carlos, prefer EnerGov's structured valuation / square-footage field** if
  it exists (see [`detail-api.md`](./detail-api.md)); fall back to description
  parsing or fees only if it doesn't. This is the main scoring improvement San
  Carlos likely enables.
- **status / near-issuance factor** — records in the "approved but not yet
  issued / not yet started" window score highest (the contractor's sweet spot).
  Driven by `STATUS_BUCKETS` (map raw status → IN_REVIEW / READY_TO_ISSUE /
  ISSUED / COMPLETE / DRAFT / UNKNOWN) and `NEAR_ISSUANCE_STATUS_FRAGMENTS`,
  plus a workflow-completion signal (`workflow_completion_pct >= 0.7 ⇒
  near-issuance`, regardless of the status label).
- **recency / liveness** — status/workflow-based liveness, *not* a hard
  N-day cutoff (cu-permits bug #19). Note San Carlos's whole corpus is recent
  (post-June-2025), so everything is "fresh" — recency barely discriminates
  until there's more history.
- **journey / clustering** — group a parcel's Planning + Building records into
  one project. Use EnerGov related-records if exposed, else address clustering
  (see [`detail-api.md`](./detail-api.md)).

The **queue pre-score** (cheap, from search-row fields only) vs the **detail
re-score** (full, after step 2) split also transfers: `predicted_lead_band`
HIGH/MEDIUM records get enrichment priority so you never burn scrape budget on
DROP-band noise. (cu-permits' `load_pending` TIER 1/2/3 ordering — port it.)

## What must be re-derived for San Carlos (recon checklist)

You **cannot** reuse Cupertino's rule tables — the type/status strings differ.
After the first scrape, harvest the real vocabulary:

```sql
-- the actual record types and their counts
SELECT record_type_detail, COUNT(*) FROM sca_permits_detail GROUP BY 1 ORDER BY 2 DESC;
-- the actual status vocabulary
SELECT record_status_detail, COUNT(*) FROM sca_permits_detail GROUP BY 1 ORDER BY 2 DESC;
-- workflow stage names (if structured)
SELECT DISTINCT stage_name FROM sca_permit_workflow;
```

Then author:

- [ ] **`type_fit_rules.json`** — map San Carlos's permit/plan types to scores.
      EnerGov type names are agency-configured; expect things like "Residential
      Building Permit", "Addition/Alteration", "New Single Family Dwelling",
      "Accessory Dwelling Unit", "Solar", "Reroof", "Mechanical", "Plumbing",
      "Electrical", plus Plan-module discretionary types. **❓ Watch for an
      explicit ADU type** — unlike Cupertino (which had none, forcing a
      description keyword split), EnerGov agencies often *do* configure a
      dedicated ADU permit type. If so, scoring is cleaner.
- [ ] **`STATUS_BUCKETS`** — map San Carlos's statuses. EnerGov common statuses:
      "Submitted", "In Review", "Plan Review", "Approved", "Ready to Issue",
      "Issued", "Finaled", "Closed", "Void". Don't copy Cupertino's map; confirm
      the real strings.
- [ ] **`NEAR_ISSUANCE_STATUS_FRAGMENTS`** — e.g. `ready to issue`, `approved`,
      `pending payment` + the `workflow_completion_pct >= 0.7` derived signal.
- [ ] **description keyword lists** — ADU / new-SFR / addition / remodel
      fragments, for splitting broad types and for category tagging.
- [ ] **size signal** — wire to EnerGov's valuation/SF field (preferred) or the
      description-SF regex fallback.

## Audit-bug fixes to carry over from cu-permits (build clean from day one)

These were hard-won across Fremont → Santa Clara → Cupertino. Apply them in
`sca-permits` from the start:

| Bug | Fix |
|---|---|
| #11 / #14 cluster key | prefer normalized address / explicit related-records; never collapse empty-owner rows |
| #16 Agent-for-Owner | "agent" in a contact role → OTHER before matching "owner" |
| #17 size factor | use a real size signal (here: EnerGov valuation/SF), fees only as fallback |
| #19 staleness | status/workflow-based liveness, not a hard 180-day cutoff |
| #20 pending payment | include in `NEAR_ISSUANCE_STATUS_FRAGMENTS` |
| #21 leads filter | filter `sca_leads` on the current strength column, not a stale one |
| #22 applicant volume | `COUNT(DISTINCT cluster)`, not raw permit count |

## Why step 3 stays separate from step 2

Same reason as cu-permits: scoring-rule changes must be cheap (seconds, no
re-scrape). Keep `step3_analyze.py` independent so re-deriving `type_fit_rules`
after you see real data is a re-run, not a re-fetch. Even though the EnerGov API
is friendlier than Accela's CF-gated detail page, the separation is still the
right design — and it's how every prior city is built.

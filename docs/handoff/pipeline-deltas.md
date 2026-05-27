# Pipeline deltas: `cu-permits` (Accela HTML) → `sca-permits` (EnerGov JSON)

Step-by-step diff for someone copying the `cu-permits` pipeline to a fresh
`sca-permits` repo. The **shape** of the pipeline is unchanged (step 0 fetch →
step 1 parse → step 2 detail → step 3 score); the **mechanics of steps 0–2** are
a rewrite because the data source is a JSON API, not an Accela WebForms site.

## Config / constants

| Constant | Cupertino (`cu-permits`) | San Carlos (`sca-permits`) |
|---|---|---|
| Platform | Accela ACA | **Tyler EnerGov CSS** |
| Base URL | `https://aca-prod.accela.com/CUPERTINO/` | `https://sancarlosca-energovweb.tylerhost.net/apps/selfservice/` |
| API style | WebForms + AJAX PageMethods | **JSON REST** |
| `AGENCY` / agencyCode | `CUPERTINO` | n/a (EnerGov uses the host, not an agencyCode param) ⚠️ confirm |
| Modules | Building, Planning | **Permit, Plan** (EnerGov module names) ⚠️ confirm |
| Table prefix | `cup_*` | `sca_*` |
| Record key | capID1/capID2/capID3 composite | **GUID `Id`** from search results |

`src/utils/config.py` keeps its *structure* (module metadata, URL builders) but
the URL builders now construct JSON-API URLs, not `CapHome/CapDetail.aspx` URLs.

---

## Step 0 — fetch search results

| Concern | Cupertino | San Carlos |
|---|---|---|
| Mechanism | `requests` postback of the `CapHome` WebForms (`__VIEWSTATE`, `__EVENTTARGET`) | **`POST` JSON** to `…/search/search` |
| Pagination | re-serialize `__VIEWSTATE`, set `Next >` event target | increment `PageNumber` in the body |
| Cache | result-grid **HTML** → `htmls/cup/{module}/` | response **JSON** → `outputs/raw/sca/{module}/` |
| Date window | `txtGSStartDate`/`txtGSEndDate` MM/DD/YYYY | `*Criteria.ApplyDateFrom/To` ISO datetimes ⚠️ confirm |
| Result cap | **~10k Accela cap → chunk by year** | **❓ unknown cap — recon it** (see `search-api.md`); reuse year-chunking only if a cap exists |
| Driver to port | `backfill_years.sh` (year-chunked) | port it **if** a cap is found; swap the Accela CLI for the JSON search |

**Rewrite:** `src/utils/step_0/fetch.py` becomes a JSON search client. Delete the
`overflow_marker` / window-halving logic and the `__VIEWSTATE` serialization —
none of it applies.

---

## Step 1 — parse search results

| Concern | Cupertino | San Carlos |
|---|---|---|
| Input | result-grid HTML | search-response JSON |
| Parser | BeautifulSoup over span-ID suffixes + row classes | **`json.loads` + field reads** over `Result.EntityResults[]` |
| Dedup key | `record_id` (from capID) | **GUID `Id`** |
| Draft filter | skip `^\d{2}TMP-` / `^DRAFT-` | ❓ EnerGov "draft"/"incomplete" status — confirm and filter on the status field, not a number regex |

**Major simplification:** `src/utils/step_1/parsing.py` shrinks dramatically —
reading JSON fields replaces HTML grid parsing. The Accela `is_tmp_draft` regex
does **not** transfer; EnerGov drafts are a *status*, not a number prefix.

---

## Step 2 — scrape details

| Piece | Cupertino (Accela) | San Carlos (EnerGov) |
|---|---|---|
| Detail fetch | GET `CapDetail.aspx?capID…` → near-empty shell | **`GET …/permit?id=<GUID>` → full JSON** |
| Session | one cookie-seeded `Session` per record (mandatory) | **stateless GET, parallel-safe** |
| Workflow | `GetProcessingData` PageMethod → parse `Marked as…` text | structured **reviews/subrecords** JSON |
| Fees | `DisplayFeeNoPaid`/`DisplayFeePaid` PageMethods | **fees** sub-resource JSON |
| Related records | `GetBuildCapTree` PageMethod | **related/associated** sub-resource ❓ (else address-cluster) |
| Contacts | **not public** (no contactinfo blocks) | **contacts** sub-resource ❓ — may expose owner/applicant/contractor |
| Inspections | **401, not anonymously available** | likely **available** via inspections sub-resource ❓ |
| Parcel/APN | AJAX-gated, effectively unobtainable | likely a **structured field** ❓ |
| Size signal | regex SF from description (no Job Value) | likely a **structured valuation/SF field** ✅-likely |
| CF handling | cooldown gate + flicker requeue + abort | **none expected** — handle 429 only if seen |

**Rewrite:** `src/utils/step_2/scrape_requests.py` becomes a JSON detail client
(GET core + sub-resource GETs, map JSON → `sca_permits_detail`). Drop the
PageMethod replay, the `{"d":…}` fragment parsing, the cookie-continuity
constraint, and the CF gate. `schema.py` `DETAIL_COLUMNS` adds a real valuation
column and (if public) contact columns; keep workflow/fees/related families.

The `cu-permits` **state/queue** logic (`step_2/state.py` `load_pending` with
its HIGH/MEDIUM-first TIER ordering and DROP-noise skip) **transfers as-is** —
it operates on the queue table, not on Accela specifics. Keep it; it's how you
enrich leads-first and conserve budget.

---

## Step 3 — analyze + score

**Architecture unchanged** (gates × factors). Re-derive the rule tables from San
Carlos vocabulary — see [`record-types-and-scoring.md`](./record-types-and-scoring.md):

- `type_fit_rules.json` — San Carlos permit/plan types (watch for a dedicated
  ADU type, unlike Cupertino).
- `STATUS_BUCKETS` — San Carlos statuses (confirm the real strings).
- **size factor → EnerGov valuation/SF field** (the main improvement over
  Cupertino's description-SF regex).
- journey/clustering — EnerGov related records if exposed, else `address_norm`.

This step barely changes structurally; only the data it reads and the rule
tables differ.

---

## Step 4 — sync to D1 (deferred, as in every city)

`sca_*` table prefix. Mirror cu-permits' tables; carry the same audit-bug fixes
(#11/#14/#16/#17/#19/#20/#21/#22 — listed in `record-types-and-scoring.md`).

---

## Things to test in v1

- [ ] Search POST returns `EntityResults` for a small Permit date window
- [ ] Pagination via `PageNumber` walks to the last page
- [ ] Result-cap behavior characterized (does a wide window clamp?)
- [ ] Step 1 reads JSON → `sca_permits`, dedups on GUID
- [ ] Draft/incomplete records filtered (by status, not number regex)
- [ ] Detail GET returns full JSON for a record id
- [ ] Workflow / fees / (contacts?) sub-resources captured
- [ ] Structured valuation/SF field found and wired to the size factor
- [ ] Contact visibility confirmed (public or not)
- [ ] History depth confirmed (does CSS return pre-June-2025 permits?)
- [ ] No CF gate needed; 429 backoff added only if observed

-- Migration 0002 — step 2 detail enrichment (sca_* prefix).
--
-- Source: a single anonymous GET per record against the EnerGov CSS route
--   GET …/api/energov/permits/permit/<CaseId>   (Route A, "raw" record)
-- verified 2026-05-27 (read-only recon). That one payload carries everything
-- scoring needs that the search row lacked:
--   * ValuationValue (numeric) — the SIZE SIGNAL Cupertino never had (it had to
--     regex SF out of the description). SquareFeet exists too but is almost
--     always 0.0, so valuation is the reliable signal.
--   * Contacts[] — owner / applicant / contractor / architect / engineer with
--     names, company, email, phone. Confirmed PUBLICLY VISIBLE for anonymous
--     users (15/15 sampled records had >=1 contact). This is the lead payload.
--   * Addresses[]/Parcels[] — structured APN.
--   * Holds[] — an open hold is a near-issuance blocker signal.
--
-- Dates (issue/expire/final) already live on sca_permits from step 1, and Route
-- A returns type/workclass as IDs (we already have the resolved names from
-- search), so Route B (permits/permit?id=) is intentionally NOT used — one GET.

-- =====================================================================
-- sca_permit_detail — 1:1 with sca_permits, populated by step 2
-- =====================================================================
CREATE TABLE IF NOT EXISTS sca_permit_detail (
    case_id            TEXT PRIMARY KEY
                         REFERENCES sca_permits(case_id) ON DELETE CASCADE,

    valuation          REAL,        -- ValuationValue (estimated project cost, $)
    square_feet        REAL,        -- SquareFeet (usually 0.0 — valuation is the signal)

    main_parcel        TEXT,        -- main address's ParcelNumber / APN
    parcel_count       INTEGER,     -- len(Parcels)

    contact_count      INTEGER,     -- len(Contacts)
    hold_count         INTEGER,     -- len(Holds) — open holds block issuance
    attachment_count   INTEGER,     -- len(Attachments)

    permit_type_id     TEXT,        -- PermitTypeID (GUID; vocab cross-ref)
    permit_workclass_id TEXT,       -- PermitWorkClassID (GUID)
    is_renewal         INTEGER,     -- IsRenewal (0/1)
    application_date   TEXT,        -- ApplicationDate (detail's own apply ts)

    -- Bookkeeping
    detail_fetched_at  TEXT,        -- when step 2 fetched this record's JSON
    detail_parsed_at   TEXT         -- when step 2 last upserted this row
);

CREATE INDEX IF NOT EXISTS idx_sca_detail_valuation ON sca_permit_detail(valuation);
CREATE INDEX IF NOT EXISTS idx_sca_detail_parcel    ON sca_permit_detail(main_parcel);

-- =====================================================================
-- sca_permit_contacts — many per case_id, populated by step 2
-- =====================================================================
-- Re-parse strategy: step 2 DELETEs all rows for a case_id then re-INSERTs, so
-- there's no fragile per-contact upsert key. ContactTypeName is explicit, but we
-- ALSO carry a normalized `role` and guard cu-permits BUG #16: an "Agent for
-- Owner" / "Owner's Agent" must classify as AGENT, never OWNER.
CREATE TABLE IF NOT EXISTS sca_permit_contacts (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    case_id            TEXT NOT NULL
                         REFERENCES sca_permits(case_id) ON DELETE CASCADE,

    parent_contact_id  TEXT,        -- ParentContactID (per-permit link id)
    global_entity_id   TEXT,        -- GlobalEntityID (global contact record)
    contact_type_id    TEXT,        -- ContactTypeID

    role_raw           TEXT,        -- ContactTypeName ("Applicant","Owner",...)
    role               TEXT,        -- normalized: OWNER/APPLICANT/CONTRACTOR/...

    first_name         TEXT,
    last_name          TEXT,
    full_name          TEXT,        -- "First Last" (trimmed)
    company            TEXT,        -- GlobalEntityName (e.g. "Thomas James Homes")

    email              TEXT,        -- EmailTo
    phone              TEXT,        -- Phone
    phone_type         TEXT,        -- PhoneType
    contact_address    TEXT,        -- MainAddress (the contact's own mailing addr)
    is_billing         INTEGER,     -- IsBilling (0/1)

    parsed_at          TEXT
);

CREATE INDEX IF NOT EXISTS idx_sca_contacts_case  ON sca_permit_contacts(case_id);
CREATE INDEX IF NOT EXISTS idx_sca_contacts_role  ON sca_permit_contacts(role);
CREATE INDEX IF NOT EXISTS idx_sca_contacts_name  ON sca_permit_contacts(full_name);
CREATE INDEX IF NOT EXISTS idx_sca_contacts_company ON sca_permit_contacts(company);

-- Migration 0001 — San Carlos permit-intel base schema (sca_* prefix).
--
-- San Carlos runs Tyler EnerGov CSS (JSON REST API), not Accela. This first
-- iteration covers step 0/1 only: the search-result row, keyed on the EnerGov
-- record GUID (CaseId). Detail (step 2: sca_permits_detail, workflow, contacts,
-- valuation) and scoring (step 3) tables are added in a later migration.
--
-- Field sources are the search response's Result.EntityResults[] objects
-- (verified 2026-05-26). CaseId is the GUID step 2's detail GET will key on —
-- the EnerGov analog of Accela's capID.

-- =====================================================================
-- sca_permits — one row per record GUID, populated by step 1 (search JSON)
-- =====================================================================
CREATE TABLE IF NOT EXISTS sca_permits (
    case_id            TEXT PRIMARY KEY,    -- EntityResults[].CaseId (GUID) — step-2 key
    case_number        TEXT NOT NULL,       -- "000003-2025", "2021-00001", "ADM2014-00001"
    module             TEXT NOT NULL,       -- "Permit" (ModuleName 2)
    module_id          INTEGER,             -- raw ModuleName int (2 = Permit)

    case_type          TEXT,                -- "Operational Permit", "Plumbing Miscellaneous", ...
    case_type_id       TEXT,
    case_workclass     TEXT,
    case_workclass_id  TEXT,
    case_status        TEXT,                -- "Cancelled", "Finaled", "In Review", "Submitted - Online", ...
    case_status_id     TEXT,

    project_name       TEXT,

    apply_date         TEXT,                -- ApplyDate (ISO from API)
    issue_date         TEXT,                -- IssueDate
    expire_date        TEXT,                -- ExpireDate
    final_date         TEXT,                -- FinalDate

    address_display    TEXT,                -- AddressDisplay (human address line)
    address_norm       TEXT,                -- canonical address for clustering (unit stripped)
    address_unit       TEXT,                -- unit kept separate (don't merge multifamily)
    main_parcel        TEXT,                -- MainParcel / APN

    description        TEXT,                -- Description

    -- Bookkeeping
    source_page        INTEGER,             -- search page this row was parsed from
    first_seen_at      TEXT,                -- first time step 1 inserted this case_id
    last_seen_at       TEXT                 -- most recent time step 1 saw it
);

CREATE INDEX IF NOT EXISTS idx_sca_permits_module     ON sca_permits(module);
CREATE INDEX IF NOT EXISTS idx_sca_permits_number     ON sca_permits(case_number);
CREATE INDEX IF NOT EXISTS idx_sca_permits_type       ON sca_permits(case_type);
CREATE INDEX IF NOT EXISTS idx_sca_permits_status     ON sca_permits(case_status);
CREATE INDEX IF NOT EXISTS idx_sca_permits_applydate  ON sca_permits(apply_date);
CREATE INDEX IF NOT EXISTS idx_sca_permits_addrnorm   ON sca_permits(address_norm);

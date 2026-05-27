-- Migration 0005 — parcel/address clustering (step 3b).
--
-- A homeowner doing a real project files several permits on one lot (demo +
-- new-SFR + ADU + the sub-trades), so per-permit leads make the GC call the
-- same parcel many times. Measured on the 2025-2026 scored set: 2,918 leads
-- collapse to ~2,023 projects (~30%). We group scored leads into a project and
-- surface ONE row per project (the call list), anchored on the strongest permit.
--
-- Cluster key (carries cu-permits bug #11/#14: prefer a real key, never collapse
-- an empty one): main_parcel APN if present, else address_norm, else the case_id
-- itself (a singleton). Caveat: a single APN can cover a multi-unit building, so
-- APN clustering can over-merge condos — acceptable here because the lead value
-- is in single-lot residential (new/ADU/addition); flagged for future refinement.

-- Link each scored lead to its project.
ALTER TABLE sca_leads ADD COLUMN cluster_id       TEXT;   -- 'P:<apn>' / 'A:<addr_norm>' / 'C:<case_id>'
ALTER TABLE sca_leads ADD COLUMN cluster_key_type TEXT;   -- PARCEL / ADDRESS / SINGLETON

CREATE INDEX IF NOT EXISTS idx_sca_leads_cluster ON sca_leads(cluster_id);

-- One row per project — the deduped call list. Rebuilt each step-3b run.
CREATE TABLE IF NOT EXISTS sca_lead_clusters (
    cluster_id        TEXT PRIMARY KEY,
    key_type          TEXT,           -- PARCEL / ADDRESS / SINGLETON
    permit_count      INTEGER,        -- scored permits in the project
    max_lead_score    REAL,           -- project strength = best permit's score
    top_band          TEXT,           -- band of the anchor permit
    categories        TEXT,           -- distinct categories across the project
    total_valuation   REAL,           -- summed valuation across the project
    max_valuation     REAL,           -- largest single permit valuation

    primary_case_id   TEXT,           -- anchor: highest score, then valuation, then newest
    address_display   TEXT,
    main_parcel       TEXT,

    -- Best outreach contact found across the project's permits.
    owner_name        TEXT,
    owner_email       TEXT,
    owner_phone       TEXT,
    has_contractor    INTEGER,        -- 1 if ANY permit in the project has a contractor

    first_apply_date  TEXT,
    last_apply_date   TEXT,
    clustered_at      TEXT
);

CREATE INDEX IF NOT EXISTS idx_sca_clusters_score ON sca_lead_clusters(max_lead_score);
CREATE INDEX IF NOT EXISTS idx_sca_clusters_band  ON sca_lead_clusters(top_band);

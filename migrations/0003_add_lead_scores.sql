-- Migration 0003 — step 3 lead scoring (sca_* prefix).
--
-- One row per scored case_id. Scoring is a multiplicative gates×factors model
-- (the cu-permits architecture, re-derived for San Carlos's real vocabulary):
--
--   lead_score = 100 · type_fit · size_factor · status_factor · contractor_factor
--
--   * type_fit         — project-type desirability for a residential GC
--                        (new SFR / ADU / addition = high; sub-trades ≈ 0).
--   * size_factor      — bucketed from EnerGov ValuationValue (the size signal
--                        Cupertino lacked); missing/0 → neutral, not zero.
--   * status_factor    — near-issuance window scores highest (Approved / Fees
--                        Due / Fees Paid); Finaled / Expired / Cancelled ≈ 0.
--   * contractor_factor— a permit with NO contractor attached is a better lead
--                        (homeowner hasn't engaged one yet) → 1.0; else 0.8.
--
-- Banded into HIGH / MEDIUM / LOW / DROP. Kept in its own table so re-scoring is
-- a seconds-long re-run (DELETE + rebuild), never a re-fetch. Best owner and
-- contractor contacts are denormalized here for direct outreach / export.
CREATE TABLE IF NOT EXISTS sca_leads (
    case_id            TEXT PRIMARY KEY
                         REFERENCES sca_permits(case_id) ON DELETE CASCADE,

    lead_score         REAL,        -- 0..100 (the four factors × 100)
    lead_band          TEXT,        -- HIGH / MEDIUM / LOW / DROP
    category           TEXT,        -- NEW_SFR/ADU/ADDITION/REMODEL/SUBTRADE/COMMERCIAL/OTHER

    type_fit           REAL,        -- 0..1
    size_factor        REAL,        -- 0..1
    status_factor      REAL,        -- 0..1
    contractor_factor  REAL,        -- 0.8 if a contractor is attached, else 1.0

    status_bucket      TEXT,        -- READY_TO_ISSUE/IN_REVIEW/ISSUED/ON_HOLD/COMPLETE/DEAD/UNKNOWN
    valuation          REAL,        -- denormalized from sca_permit_detail (sort key)
    has_contractor     INTEGER,     -- 1 if a CONTRACTOR contact is attached

    -- Best contacts for outreach (denormalized from sca_permit_contacts)
    owner_name         TEXT,        -- owner, else applicant
    owner_email        TEXT,
    owner_phone        TEXT,
    contractor_name    TEXT,        -- company, else full name

    scored_at          TEXT
);

CREATE INDEX IF NOT EXISTS idx_sca_leads_band  ON sca_leads(lead_band);
CREATE INDEX IF NOT EXISTS idx_sca_leads_score ON sca_leads(lead_score);
CREATE INDEX IF NOT EXISTS idx_sca_leads_cat   ON sca_leads(category);

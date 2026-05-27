-- Migration 0004 — harvest scope + holds data already present in the cached
-- detail JSON (no re-fetch; a re-parse populates these).
--
-- Discovered in the step 0-3 audit (2026-05-27):
--   * CustomFields carry agency scope data on building permits — but the labels
--     have TRAILING SPACES ('Number of Stories '), so the parser must strip keys.
--       - Additional Square Footage  (228 records) — a secondary SIZE signal,
--         used as a fallback when ValuationValue is missing/0 (bug #17 spirit).
--       - Number of Stories / Type of Construction / Occupancy Class
--         (532 / 604 / 501) — lead-qualification context for the call.
--   * Holds carry an `Active` flag and a type name. ~315 active holds exist;
--     most are "Expired Permit Hold" (mirrors Expired status, ignore), but
--     Stop Work / Planning / Property / General holds on a live permit are
--     near-issuance BLOCKERS. We split count into active vs. blocking.

ALTER TABLE sca_permit_detail ADD COLUMN additional_sqft     REAL;    -- CustomField "Additional Square Footage"
ALTER TABLE sca_permit_detail ADD COLUMN num_stories         REAL;    -- CustomField "Number of Stories"
ALTER TABLE sca_permit_detail ADD COLUMN construction_type   TEXT;    -- CustomField "Type of Construction"
ALTER TABLE sca_permit_detail ADD COLUMN occupancy_class     TEXT;    -- CustomField "Occupancy Class"
ALTER TABLE sca_permit_detail ADD COLUMN active_hold_count    INTEGER; -- Holds where Active=true
ALTER TABLE sca_permit_detail ADD COLUMN blocking_hold_count  INTEGER; -- active holds excluding "Expired Permit Hold"

-- Context denormalized onto the lead for outreach + the D1 export.
ALTER TABLE sca_leads ADD COLUMN additional_sqft   REAL;
ALTER TABLE sca_leads ADD COLUMN num_stories       REAL;
ALTER TABLE sca_leads ADD COLUMN construction_type TEXT;
ALTER TABLE sca_leads ADD COLUMN blocking_hold     INTEGER;  -- 1 if a non-expired active hold is present

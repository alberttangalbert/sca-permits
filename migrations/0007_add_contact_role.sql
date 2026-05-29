-- Migration 0007 — extend the lead outreach contact past OWNER/APPLICANT.
--
-- Audit 2026-05-29 found ~10 actionable (HIGH/MEDIUM) leads marked "unreachable"
-- despite the underlying permit having a reachable ARCHITECT or AGENT contact:
--
--   BLD2024-00118  HIGH NEW_SFR    OWNER=SANJAY ZALAVADIA (no contact)
--                                   ARCHITECT=MILLER JIM, phone 510-594-1814
--   BLD2024-00727  MED  ADDITION   OWNER=ROBERT J BRADY (no contact)
--                                   ARCHITECT=JANG ARCHITECT JON, email+phone
--   ...
--
-- For residential design-build, the architect is a perfectly valid first-call
-- POC (forwards to homeowner; or directly hires for the construction phase).
-- The applicant fall-through (#16 era) already established the precedent of
-- using a non-owner contact when the owner row is contactless; this extends
-- that chain to ARCHITECT/AGENT (in that order of reliability) as a third tier.
--
-- The new column LABELS where the picked contact came from so the GC's call
-- list can show "MILLER JIM (Architect)" instead of misrepresenting the
-- architect as the homeowner. NULL = OWNER (the existing default).

ALTER TABLE sca_leads
    ADD COLUMN contact_role TEXT;  -- 'OWNER'/'APPLICANT'/'ARCHITECT'/'AGENT'/NULL

CREATE INDEX IF NOT EXISTS idx_sca_leads_contact_role ON sca_leads(contact_role);

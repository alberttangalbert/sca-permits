-- Migration 0009 — carry the real property-owner NAME even when unreachable.
--
-- Audit 2026-06-01 found 71 actionable (HIGH/MEDIUM/LOW) leads whose surfaced
-- outreach contact is a non-owner (designer / applicant / architect) while a
-- real, NAMED owner row existed on the permit but had no email/phone -- so it
-- lost the OWNER tier in pick_contacts and was dropped from the lead entirely:
--
--   F927A399  surfaced APPLICANT "Natalie Hyland" (Hyland Design Group)
--             real owner row: "JAMES L WATERBURY" (no phone/email)
--   445B484B  surfaced ARCHITECT "MILLER JIM"
--             real owner row: "SANJAY ZALAVADIA" (no phone/email)
--
-- owner_name holds the chosen *outreach* contact (who to call first), which is
-- correctly the reachable designer/architect. But the homeowner's NAME is still
-- lead-collection signal: paired with the address (already exported) the GC can
-- reverse-lookup a phone, send direct mail, or door-knock knowing who lives
-- there. This column carries that name independently of the outreach pick.
-- NULL = no named owner row on the permit (or the owner WAS the outreach pick).

ALTER TABLE sca_leads
    ADD COLUMN property_owner_name TEXT;

ALTER TABLE sca_lead_clusters
    ADD COLUMN property_owner_name TEXT;

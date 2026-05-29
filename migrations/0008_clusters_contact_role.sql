-- Migration 0008 — propagate contact_role into sca_lead_clusters.
--
-- Migration 0007 added contact_role to sca_leads so the GC's per-permit call
-- list could label e.g. "MILLER JIM (Architect)" instead of mislabeling the
-- architect as the homeowner. But the DEDUPED call list (sca_lead_clusters,
-- one row per project) still surfaced owner_name/email/phone without the
-- role label, losing the disambiguation on a heavily-used export. Bring
-- clusters into parity so the cluster export is as honest as the per-permit
-- one. cluster.contact_role echoes the role of the cluster's chosen
-- outreach-contact MEMBER (the one aggregate() picks for owner_*).

ALTER TABLE sca_lead_clusters
    ADD COLUMN contact_role TEXT;  -- mirrors sca_leads.contact_role on the picked member

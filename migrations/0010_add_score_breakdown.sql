-- Migration 0010 — add the serialized score_breakdown JSON to sca_leads.
--
-- San Carlos scores via a 6-factor multiplicative model
-- (type_fit·size·status·contractor·hold·recency) but, like Danville, only ever
-- shipped the final lead_score/lead_band to D1 — the per-factor decomposition
-- was computed in scoring.score_record and discarded. So the frontend "Why this
-- lead" panel was BLANK for San Carlos, unlike Fremont (full breakdown) /
-- San José / Santa Clara (synthesized from subscores).
--
-- This column carries a Fremont-shape JSON object
--   {formula, lead_score, strength, factors[], gates[], flags}
-- with the 6 factors. The frontend FACTOR_META was extended with the EnerGov
-- ids (status/contractor/hold) so it renders. Built by
-- scoring.build_score_breakdown; surfaced by web functions/_shared/queries/
-- sancarlos.ts. A re-score (step3 --rebuild) repopulates it; no re-fetch needed.
-- Mirrors dan-permits migration 0011 (the two EnerGov cities share the scorer).

ALTER TABLE sca_leads ADD COLUMN score_breakdown TEXT;  -- JSON: factor decomposition for the UI

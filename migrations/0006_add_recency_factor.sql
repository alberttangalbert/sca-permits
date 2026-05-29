-- Migration 0006 — add a recency-decay factor to the lead score.
--
-- Discovered in the backfill audit (2026-05-29): the gates×factors model judged
-- liveness purely by workflow STATUS (cu-permits bug #19's "no hard day cutoff"
-- doctrine). That holds for a live-from-day-one system, but San Carlos CSS
-- carries MIGRATED history: thousands of pre-cutover permits froze at an
-- actionable status ("Approved") and never advanced to "Finaled", so a 2004
-- approved SFR scored identically to a 2026 one. Result: 28% of the HIGH/MEDIUM
-- actionable funnel were permits filed >5y ago — dead jobs, not leads.
--
-- Fix: a sixth multiplicative factor that decays with permit age (apply_date),
-- so stale records gently fall out of HIGH/MEDIUM without being hard-deleted.
-- Taper (user-chosen 2026-05-29): <=1y 1.0 / 1-2y 0.8 / 2-3y 0.55 / 3-5y 0.3 / >5y 0.1.
-- A re-score (step3 --rebuild) repopulates this column; no re-fetch needed.

ALTER TABLE sca_leads ADD COLUMN recency_factor REAL;  -- 0..1, age-based decay on apply_date

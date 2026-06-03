"""Regression tests for the pure pipeline logic (no DB, no network).

The scoring/clustering behavior is driven by editable rule tables, so these lock
in the decisions that matter — especially the hard-won bug guards:
  * #16  an "Agent for Owner" must classify AGENT, never OWNER
  * the trailing-space CustomField labels ('Number of Stories ')
  * SQL-literal escaping of quotes in the D1 export

Run:  python3 -m unittest discover -s tests   (or: python3 -m unittest tests.test_logic)
"""

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from utils.normalize import split_address
from utils.step_1.parsing import map_entity, parse_page
from utils.step_2.parsing import (_custom_fields, _fnum, _holds_summary,
                                   normalize_role, parse_detail, parse_contacts)
from utils.step_3.clustering import (aggregate, canonical_parcel_by_address,
                                      cluster_key)
from utils.step_3.scoring import (band, classify_type, pick_contacts,
                                   recency_factor, score_record, size_factor,
                                   status_factor)
import sqlite3

import datetime as dt

from step2_fetch_details import select_case_ids
from step2_parse_details import unparsed_files, _read_cached_detail
from step1_parse_search_results import _parse_pages
from step0b_refresh_stale import (stale_apply_days, refresh_floor,
                                   _at_risk_apply_dates, fetch_day_windows,
                                   MAX_CONSECUTIVE_ERRORS)
from utils.step_0.fetch import SearchError
from step4_sync_d1 import (_lit, _prune_statement, _fetch_rows, _csv_rows,
                           LEADS_SPEC, CLUSTERS_SPEC, _reengagement_rows,
                           REENGAGEMENT_COLUMNS)
from tick import (should_refresh, should_skip_entirely, refresh_state_action,
                  escalated_backoff_minutes, OUTAGE_BACKOFF_MINUTES,
                  MAX_OUTAGE_BACKOFF_MINUTES, _should_reclaim_lock,
                  LOCK_STALE_SECONDS)
from utils.step_2 import detail as detailmod
from utils.step_0 import fetch as fetchmod
from utils.io import apply_migrations, load_run_ledger, LEDGER_MAX_RUNS
from healthcheck import migration_set_status


class TypeFit(unittest.TestCase):
    def test_new_sfr_and_adu_are_top(self):
        self.assertEqual(classify_type("Building Residential-New Single Family", None),
                         (1.0, "NEW_SFR"))
        self.assertEqual(classify_type("Building Residential-Second Dwelling Unit", None),
                         (1.0, "ADU"))

    def test_addition_vs_commercial_order(self):
        # Residential addition scores high...
        self.assertEqual(classify_type("Building Residential- Addition", None),
                         (0.9, "ADDITION"))
        # ...but a COMMERCIAL addition must hit the commercial rule first, not
        # be read as a residential addition.
        self.assertEqual(classify_type("Building Commercial-Addition", None),
                         (0.2, "COMMERCIAL"))

    def test_apartment_scores_as_multifamily(self):
        # Apartment/multi-family permits are real GC work, scored like the
        # near-identical 'multi-residential' type (0.5), not the 0.30 fallback.
        self.assertEqual(classify_type("Apartment  - Apartment", None),
                         (0.5, "COMMERCIAL"))
        self.assertEqual(classify_type("Building Residential-Multi-Residential", None),
                         (0.5, "COMMERCIAL"))

    def test_subtrades_are_low(self):
        for t in ("MEP - Solar App", "Building Residential - Reroof",
                  "Electrical Service", "Water Heater"):
            fit, cat = classify_type(t, None)
            self.assertLessEqual(fit, 0.15, t)
            self.assertEqual(cat, "SUBTRADE", t)

    def test_misc_description_upgrade_only_for_other(self):
        # A misc permit whose description names an ADU gets upgraded...
        self.assertEqual(
            classify_type("Building Residential-Miscellaneous", "NEW ADU IN REAR YARD"),
            (1.0, "ADU"))
        # ...but a plain misc stays at its low base.
        self.assertEqual(
            classify_type("Building Residential-Miscellaneous", "REPLACE STUCCO"),
            (0.25, "OTHER"))
        # An explicit sub-trade is NOT upgraded by a stray description word.
        fit, cat = classify_type("Building Residential - Reroof", "reroof over addition")
        self.assertEqual((fit, cat), (0.1, "SUBTRADE"))

    def test_additional_does_not_false_upgrade_to_addition(self):
        # 'additional' is an adjective ('extra'), not a build-out: a bathroom
        # remodel must NOT inflate to ADDITION (0.9) off the substring (audit
        # 2026-06-02). It should land REMODEL via the 'remodel' keyword.
        self.assertEqual(
            classify_type("Building Residential-Miscellaneous",
                          "Bathroom remodeling AND ADDITIONAL ELEMENTS PER CODE"),
            (0.6, "REMODEL"))
        # ...and with no other keyword, 'additional' alone leaves it at base.
        self.assertEqual(
            classify_type("Building Residential-Miscellaneous",
                          "additional required elements per code"),
            (0.25, "OTHER"))

    def test_real_addition_singular_and_plural_still_upgrade(self):
        # The fix must not regress genuine additions, singular OR plural.
        self.assertEqual(
            classify_type("Building Residential-Miscellaneous",
                          "703 SF ADDITION to the residence"),
            (0.9, "ADDITION"))
        self.assertEqual(
            classify_type("Building Residential-Miscellaneous",
                          "two-story additions at front and rear"),
            (0.9, "ADDITION"))

    def test_accessory_structure_is_its_own_category(self):
        # A plain accessory structure (patio cover / detached studio / retaining
        # wall) gets the ACCESSORY label, not the OTHER catch-all — same 0.7 fit.
        self.assertEqual(
            classify_type("Building Residential-Accessory Structure",
                          "Attached Patio Cover; Freestanding Patio Cover"),
            (0.7, "ACCESSORY"))

    def test_accessory_structure_still_upgrades_to_specific_category(self):
        # Score-neutrality guard: an accessory structure whose description names a
        # real ADU must STILL promote to ADU (fit 1.0), exactly as it did when
        # accessory structures lived in OTHER — relabeling the base category must
        # not silently drop these leads' scores.
        self.assertEqual(
            classify_type("Building Residential-Accessory Structure",
                          "NEW DETACHED ADU, 500-SQFT"),
            (1.0, "ADU"))
        # A commercial accessory structure still reads COMMERCIAL (the 'commercial'
        # rule precedes 'accessory structure' in the ordered ruleset).
        self.assertEqual(
            classify_type("Building Commercial-Accessory Structure", "tent"),
            (0.2, "COMMERCIAL"))


class Roles(unittest.TestCase):
    def test_agent_for_owner_is_agent_not_owner(self):  # bug #16
        self.assertEqual(normalize_role("Agent for Owner"), "AGENT")
        self.assertEqual(normalize_role("Owner's Agent"), "AGENT")

    def test_plain_roles(self):
        self.assertEqual(normalize_role("Owner"), "OWNER")
        self.assertEqual(normalize_role("General Contractor"), "CONTRACTOR")
        self.assertEqual(normalize_role("Applicant"), "APPLICANT")
        self.assertIsNone(normalize_role(None))

    def test_designer_is_first_class_role(self):
        # 'Designer' was the biggest occupant of the OTHER bucket pre-fix
        # (1,623 contacts, 76% email-reachable). Promote it to a real role
        # so the contact chain can use it on residential design-build leads.
        self.assertEqual(normalize_role("Designer"), "DESIGNER")
        self.assertEqual(normalize_role("Interior Designer"), "DESIGNER")


class Factors(unittest.TestCase):
    def test_size_factor_buckets(self):
        self.assertEqual(size_factor(None), 0.35)   # missing != zero
        self.assertEqual(size_factor(0), 0.35)
        self.assertEqual(size_factor(3000), 0.2)
        self.assertEqual(size_factor(30000), 0.5)
        self.assertEqual(size_factor(249999), 0.8)
        self.assertEqual(size_factor(250000), 0.9)
        self.assertEqual(size_factor(600000), 1.0)

    def test_status_buckets(self):
        self.assertEqual(status_factor("Approved"), (1.0, "READY_TO_ISSUE"))
        self.assertEqual(status_factor("Fees Due"), (1.0, "READY_TO_ISSUE"))
        self.assertEqual(status_factor("In Review"), (0.7, "IN_REVIEW"))
        self.assertEqual(status_factor("Issued"), (0.45, "ISSUED"))
        self.assertEqual(status_factor("Finaled"), (0.05, "COMPLETE"))
        self.assertEqual(status_factor("Expired"), (0.03, "DEAD"))
        self.assertEqual(status_factor("Nonsense"), (0.4, "UNKNOWN"))

    def test_description_void_override_dead(self):
        # Real-world case (BLDR2026-00220, BLDR2026-00057): permit's status is
        # still 'Submitted - Online' but the description has been edited to
        # 'VOID WRONG PERMIT TYPE ...' by a city worker who forgot to flip
        # status. The lead must NOT ride that status into MEDIUM/HIGH.
        scored = score_record(
            case_type="Building Residential-New Single Family",
            case_status="Submitted - Online",
            description="VOID WRONG PERMIT TYPE 2(two) heat pump installation",
            valuation=600000, contacts=[])
        self.assertEqual(scored["status_bucket"], "DEAD")
        self.assertEqual(scored["lead_band"], "DROP")

    def test_negative_valuation_stored_as_null(self):
        # EnerGov has 3 records with valuation < 0 (data-entry typos on small
        # 2020 sub-trade permits). size_factor already treats them as neutral;
        # the lead row should also NULL them out so D1 doesn't show "-$9".
        scored = score_record(
            case_type="Water Heater", case_status="Finaled",
            description="water heater replacement", valuation=-9.0, contacts=[])
        self.assertIsNone(scored["valuation"])
        # And zero stays neutral but also NULL'd on the row (consistency).
        scored = score_record(
            case_type="Plumbing", case_status="Issued",
            description="x", valuation=0.0, contacts=[])
        self.assertIsNone(scored["valuation"])

    def test_description_void_does_not_override_complete(self):
        # If a permit ALREADY completed (Finaled), a void-looking description
        # is just historical -- don't override the COMPLETE bucket. The 0.05
        # COMPLETE factor is already low enough; further override is noise.
        scored = score_record(
            case_type="Building Residential-New Single Family",
            case_status="Finaled",
            description="VOID WRONG PERMIT TYPE (original record)",
            valuation=600000, contacts=[])
        self.assertEqual(scored["status_bucket"], "COMPLETE")

    def test_band_boundaries(self):
        self.assertEqual(band(0.50), "HIGH")
        self.assertEqual(band(0.499), "MEDIUM")
        self.assertEqual(band(0.22), "MEDIUM")
        self.assertEqual(band(0.219), "LOW")
        self.assertEqual(band(0.07), "LOW")
        self.assertEqual(band(0.069), "DROP")


class Contacts(unittest.TestCase):
    def test_prefers_owner_with_contact_and_flags_contractor(self):
        contacts = [
            {"role": "OWNER", "full_name": "Jane Doe", "company": None,
             "email": "jane@x.com", "phone": "555-1234567"},
            {"role": "APPLICANT", "full_name": "Appy", "company": None,
             "email": None, "phone": None},
            {"role": "CONTRACTOR", "full_name": "Bob", "company": "BuildCo",
             "email": None, "phone": "555-7654321"},
        ]
        picked = pick_contacts(contacts)
        self.assertEqual(picked["owner_name"], "Jane Doe")
        self.assertEqual(picked["owner_email"], "jane@x.com")
        self.assertEqual(picked["has_contractor"], 1)
        self.assertEqual(picked["contractor_name"], "BuildCo")

    def test_applicant_used_when_no_owner(self):
        picked = pick_contacts([
            {"role": "APPLICANT", "full_name": "Appy", "company": None,
             "email": "a@x.com", "phone": None}])
        self.assertEqual(picked["owner_name"], "Appy")
        self.assertEqual(picked["has_contractor"], 0)
        self.assertIsNone(picked["contractor_name"])

    def test_contactless_owner_falls_through_to_reachable_applicant(self):
        # Owner row exists but has only a name; the applicant holds the phone.
        # We must surface the reachable applicant, not strand the lead.
        picked = pick_contacts([
            {"role": "OWNER", "full_name": "Nameonly Owner", "email": None,
             "phone": None},
            {"role": "APPLICANT", "full_name": "Reachable App", "email": None,
             "phone": "555-1234567"}])
        self.assertEqual(picked["owner_phone"], "5551234567")
        self.assertEqual(picked["owner_name"], "Reachable App")
        # ...but the real homeowner's NAME must NOT be lost: it rides along on
        # property_owner_name so the GC can still reverse-lookup / mail / knock.
        self.assertEqual(picked["property_owner_name"], "Nameonly Owner")

    def test_property_owner_name_independent_of_outreach_pick(self):
        # The surfaced outreach contact is a reachable designer; the owner row is
        # a different, contactless person. owner_name = the designer (who to call
        # first), property_owner_name = the actual homeowner.
        picked = pick_contacts([
            {"role": "OWNER", "full_name": "James Waterbury", "email": None,
             "phone": None},
            {"role": "DESIGNER", "full_name": "Natalie Hyland",
             "company": "Hyland Design Group", "email": "info@hylanddg.com",
             "phone": "555-9999999"}])
        self.assertEqual(picked["owner_name"], "Natalie Hyland")
        self.assertEqual(picked["contact_role"], "DESIGNER")
        self.assertEqual(picked["property_owner_name"], "James Waterbury")

    def test_property_owner_name_none_when_no_owner_row(self):
        # No OWNER role on the permit -> nothing to carry (not the designer's name).
        picked = pick_contacts([
            {"role": "APPLICANT", "full_name": "Some Applicant",
             "email": "a@x.com", "phone": None}])
        self.assertIsNone(picked["property_owner_name"])

    def test_property_owner_name_ignores_placeholder_owner(self):
        # An EnerGov-conversion placeholder owner is not a real name.
        picked = pick_contacts([
            {"role": "OWNER", "full_name": "EnerGov 2023Q4",
             "company": "EnerGov Conversion",
             "email": "energovconversion2023Q4@tylertech.com", "phone": None},
            {"role": "APPLICANT", "full_name": "Real App", "email": "a@x.com",
             "phone": None}])
        self.assertIsNone(picked["property_owner_name"])

    def test_property_owner_name_ignores_placeholder_company_blank_name(self):
        # Defense-in-depth: property_owner_name falls back to a contact's COMPANY
        # when full_name is blank, so a placeholder company ('EnerGov Conversion')
        # on a name-less OWNER row must still be filtered (else it leaks past the
        # name-only check). Independent review, 2026-06-02.
        picked = pick_contacts([
            {"role": "OWNER", "full_name": "", "company": "EnerGov Conversion",
             "email": None, "phone": None},
            {"role": "APPLICANT", "full_name": "Real App", "email": "a@x.com",
             "phone": None}])
        self.assertIsNone(picked["property_owner_name"])

    def test_reachable_owner_still_wins_over_applicant(self):
        # When the owner IS reachable, keep preferring the owner.
        picked = pick_contacts([
            {"role": "OWNER", "full_name": "Owner", "email": "o@x.com", "phone": None},
            {"role": "APPLICANT", "full_name": "App", "email": "a@x.com",
             "phone": "555-1234567"}])
        self.assertEqual(picked["owner_name"], "Owner")
        self.assertEqual(picked["owner_email"], "o@x.com")

    def test_void_void_placeholder_does_not_surface_as_owner(self):
        # EnerGov's redacted-applicant marker: it has no real identity, so it
        # must not be picked as the lead's owner. Fall through to real contacts.
        picked = pick_contacts([
            {"role": "APPLICANT", "full_name": "void void", "email": None,
             "phone": None},
            {"role": "OWNER", "full_name": "Real Owner", "email": "r@x.com",
             "phone": "555-1234567"}])
        self.assertEqual(picked["owner_name"], "Real Owner")
        self.assertEqual(picked["owner_email"], "r@x.com")

    def test_void_void_only_contact_yields_unreachable_lead(self):
        # When the ONLY candidate is a placeholder, the lead is correctly
        # marked unreachable (no garbage name surfaced) — the healthcheck WARN
        # will count it as a source-data gap, which is the accurate picture.
        picked = pick_contacts([
            {"role": "APPLICANT", "full_name": "void void", "email": None,
             "phone": None}])
        self.assertIsNone(picked["owner_name"])
        self.assertIsNone(picked["owner_email"])
        self.assertIsNone(picked["owner_phone"])

    def test_architect_recovers_unreachable_owner(self):
        # Real-world case (BLD2024-00727): residential addition with an OWNER
        # name but no contact, and a reachable ARCHITECT. The lead would have
        # been silently unreachable; now the architect surfaces as the contact
        # and contact_role labels it so the GC knows who they're calling.
        picked = pick_contacts([
            {"role": "OWNER", "full_name": "ROBERT J BRADY", "email": None,
             "phone": None},
            {"role": "ARCHITECT", "full_name": "JANG ARCHITECT JON",
             "email": "f@jangarchitect.com", "phone": "555-arc"}])
        self.assertEqual(picked["owner_name"], "JANG ARCHITECT JON")
        self.assertEqual(picked["owner_email"], "f@jangarchitect.com")
        self.assertEqual(picked["contact_role"], "ARCHITECT")

    def test_agent_used_after_architect(self):
        # Order: OWNER -> APPLICANT -> ARCHITECT -> DESIGNER -> ENGINEER -> AGENT.
        # Agent only wins when no higher-tier reachable contact is available.
        picked = pick_contacts([
            {"role": "AGENT", "full_name": "Agent A", "email": "a@x.com", "phone": None},
            {"role": "ARCHITECT", "full_name": "Arch A", "email": "arc@x.com", "phone": None}])
        self.assertEqual(picked["contact_role"], "ARCHITECT")
        self.assertEqual(picked["owner_email"], "arc@x.com")

    def test_designer_recovers_unreachable_owner(self):
        # Real-world case (BLD2024-00925): residential ADDITION with contactless
        # OWNER and a Designer contact with full email+phone. Pre-fix the
        # designer was bucketed into OTHER and the lead stayed unreachable.
        picked = pick_contacts([
            {"role": "OWNER", "full_name": "DEREK FUNG", "email": None, "phone": None},
            {"role": "DESIGNER", "full_name": "Lu Xin",
             "email": "info@orengr.com", "phone": "555"}])
        self.assertEqual(picked["contact_role"], "DESIGNER")
        self.assertEqual(picked["owner_email"], "info@orengr.com")

    def test_chain_skips_unreachable_designer_to_engineer(self):
        # The chain stops at the first REACHABLE tier, not just the first tier
        # that exists. A named-but-contactless designer doesn't block engineer.
        picked = pick_contacts([
            {"role": "DESIGNER", "full_name": "Named Designer", "email": None,
             "phone": None},
            {"role": "ENGINEER", "full_name": "Reachable Engineer",
             "email": "e@x.com", "phone": None}])
        self.assertEqual(picked["contact_role"], "ENGINEER")

    def test_garbage_email_does_not_beat_real_phone(self):
        # Real-world case: an OWNER row has a typo email like 'a@b@c.com' (two
        # @'s) and no phone, while a DESIGNER row has a real phone. Without
        # validation, the OWNER would win the ranking on (email=True, ...)
        # despite the email being unusable, and the lead would surface as
        # "reachable" via a garbage address. After the fix, garbage email is
        # treated as missing and the DESIGNER's real phone wins.
        picked = pick_contacts([
            {"role": "OWNER", "full_name": "Typo Owner",
             "email": "a@b@c.com", "phone": None},
            {"role": "DESIGNER", "full_name": "Real Designer",
             "email": None, "phone": "555-1234567"}])
        self.assertEqual(picked["contact_role"], "DESIGNER")
        self.assertIsNone(picked["owner_email"])
        self.assertEqual(picked["owner_phone"], "5551234567")

    def test_too_short_phone_is_dropped(self):
        # Genuinely-short junk (<7 digits) can't be recovered to a real number,
        # so it's treated as missing and the lead falls through to a reachable
        # contact. (7-digit numbers ARE recovered -- see the next test.)
        picked = pick_contacts([
            {"role": "OWNER", "full_name": "Local-only", "email": None,
             "phone": "555-12"},
            {"role": "APPLICANT", "full_name": "Reachable",
             "email": "a@b.com", "phone": "(650) 555-1234"}])
        self.assertEqual(picked["contact_role"], "APPLICANT")
        self.assertEqual(picked["owner_email"], "a@b.com")

    def test_seven_digit_phone_is_recovered(self):
        # San Carlos is wholly area code 650, so a 7-digit owner phone (area
        # code stripped by the legacy EnerGov import) is recovered by prepending
        # 650 rather than dropped -- the owner stays reachable and is picked.
        picked = pick_contacts([
            {"role": "OWNER", "full_name": "Local-only", "email": None,
             "phone": "555-1234"},
            {"role": "APPLICANT", "full_name": "Reachable",
             "email": "a@b.com", "phone": "(650) 555-1234"}])
        self.assertEqual(picked["contact_role"], "OWNER")
        self.assertEqual(picked["owner_phone"], "6505551234")

    def test_phone_in_email_field_doesnt_count_as_email(self):
        # EnerGov sometimes has '5302210761' in the email field (data entry
        # bug). Strict validation rejects it as an email, so the architect
        # wins on real reachability and the bogus 'email' never gets surfaced.
        picked = pick_contacts([
            {"role": "OWNER", "full_name": "Bad Data",
             "email": "5302210761", "phone": None},
            {"role": "ARCHITECT", "full_name": "Real Architect",
             "email": "arc@x.com", "phone": "555-1234567"}])
        self.assertEqual(picked["contact_role"], "ARCHITECT")
        self.assertEqual(picked["owner_email"], "arc@x.com")

    def test_contact_role_is_owner_when_owner_reachable(self):
        # When the owner is genuinely reachable, contact_role labels it OWNER --
        # the UI doesn't show "Architect" when the owner picked the phone up.
        picked = pick_contacts([
            {"role": "OWNER", "full_name": "Real Owner", "email": "o@x", "phone": None},
            {"role": "ARCHITECT", "full_name": "Arch", "email": "a@x", "phone": "555-2"}])
        self.assertEqual(picked["contact_role"], "OWNER")

    def test_contact_role_none_when_no_contact_anywhere(self):
        # Empty contacts list -> nothing to surface, contact_role is None
        # (not a misleading 'OWNER' default).
        picked = pick_contacts([])
        self.assertIsNone(picked["contact_role"])
        self.assertIsNone(picked["owner_name"])

    def test_energov_migration_placeholder_filtered(self):
        # 'EnerGov YYYYQN' is Tyler's migration-era stamp (paired with the
        # energovconversion@tylertech.com email). 2,371 contacts use this name
        # spanning 2002-2025 quarters. Filter them so they never surface as a
        # lead's owner -- the lead falls through to a real contact (if any).
        picked = pick_contacts([
            {"role": "OWNER", "full_name": "EnerGov 2009Q2",
             "email": "energovconversion2009Q2@tylertech.com", "phone": None},
            {"role": "ARCHITECT", "full_name": "Real Architect",
             "email": "arc@x.com", "phone": "555-1234567"}])
        self.assertEqual(picked["contact_role"], "ARCHITECT")
        self.assertEqual(picked["owner_name"], "Real Architect")
        # Also matches a 2024 quarter (the migration stamps keep appearing
        # on recent sync events too, not just 2002-2010 legacy).
        picked2 = pick_contacts([
            {"role": "APPLICANT", "full_name": "EnerGov 2024Q3",
             "email": "x@x.com", "phone": "555-1111111"}])
        self.assertIsNone(picked2["owner_name"])
        self.assertIsNone(picked2["contact_role"])

    def test_builder_owner_contractor_is_not_real_competition(self):
        # 'BUILDER OWNER' is the owner-builder stamp (homeowner acting as their
        # own contractor). It's NOT a hired contractor in the way that signals
        # "the GC already lost this lead" -> filter it from the contractor pool
        # too. has_contractor stays 0 so the owner-builder permit doesn't get
        # the 0.8 contractor-penalty applied -> these stay high-quality leads.
        picked = pick_contacts([
            {"role": "OWNER", "full_name": "Real Owner", "email": "r@x",
             "phone": "555-r"},
            {"role": "CONTRACTOR", "full_name": "BUILDER OWNER",
             "email": None, "phone": "9253674813"}])
        self.assertEqual(picked["has_contractor"], 0)
        self.assertIsNone(picked["contractor_name"])
        self.assertEqual(picked["owner_name"], "Real Owner")


class ScoreRecord(unittest.TestCase):
    def test_blocking_hold_and_contractor_factors(self):
        base = dict(case_type="Building Residential-New Single Family",
                    case_status="Approved", description=None, valuation=600000,
                    contacts=[{"role": "OWNER", "full_name": "O", "email": "o@x",
                               "phone": "1"}])
        clean = score_record(**base)
        self.assertEqual(clean["lead_score"], 1.0)          # 1*1*1*1 (normalized [0,1])
        self.assertEqual(clean["lead_band"], "HIGH")
        # A contractor present and a blocking hold each shave the score.
        withc = score_record(**{**base, "contacts": [
            {"role": "CONTRACTOR", "company": "C"}]}, blocking_hold_count=1)
        self.assertEqual(withc["blocking_hold"], 1)
        self.assertAlmostEqual(withc["lead_score"], 0.8 * 0.9, places=4)

    def test_recency_decays_stale_migrated_permit(self):
        # Same approved new-SFR, scored fresh vs. 20 years stale: fresh stays HIGH,
        # ancient frozen-status record falls out of the actionable funnel.
        base = dict(case_type="Building Residential-New Single Family",
                    case_status="Approved", description=None, valuation=600000,
                    contacts=[{"role": "OWNER", "full_name": "O", "email": "o@x"}])
        today = dt.date(2026, 5, 29)
        fresh = score_record(**base, apply_date="2026-03-01", today=today)
        self.assertEqual(fresh["recency_factor"], 1.0)
        self.assertEqual(fresh["lead_band"], "HIGH")
        stale = score_record(**base, apply_date="2004-04-01", today=today)
        self.assertEqual(stale["recency_factor"], 0.1)
        self.assertEqual(stale["lead_score"], 0.1)    # 1.0 * 0.1 (normalized [0,1])
        self.assertEqual(stale["lead_band"], "LOW")   # out of HIGH/MEDIUM


class ScoreBreakdownJSON(unittest.TestCase):
    """score_breakdown JSON powers the frontend "Why this lead" panel. Lock in
    the contract so a scoring change can't silently break it: valid JSON, the
    ScoreBreakdown shape, factor ids the frontend FACTOR_META knows, factors that
    multiply to the score, and a strength matching the band. (Mirrors
    dan-permits' ScoreBreakdownJSON — shared EnerGov scorer.)"""

    # Must stay in lockstep with web/src/lib/api/types.ts FactorId.
    _FRONTEND_FACTOR_IDS = {"warmth", "type_fit", "recency", "size",
                            "status", "contractor", "hold"}

    def _bd(self, **over):
        base = dict(case_type="Building Residential-New Single Family",
                    case_status="Approved", description="New SFR", valuation=600000,
                    contacts=[{"role": "OWNER", "full_name": "O", "email": "o@x",
                               "phone": "6505551234"}])
        lead = score_record(**{**base, **over})
        return lead, json.loads(lead["score_breakdown"])

    def test_shape_and_keys(self):
        _, bd = self._bd()
        self.assertEqual(set(bd), {"formula", "lead_score", "strength",
                                   "factors", "gates", "flags"})
        self.assertEqual(len(bd["factors"]), 6)

    def test_factor_ids_known_to_frontend(self):
        _, bd = self._bd()
        for f in bd["factors"]:
            self.assertIn(f["factor"], self._FRONTEND_FACTOR_IDS)

    def test_factors_multiply_to_score(self):
        lead, bd = self._bd()
        prod = 1.0
        for f in bd["factors"]:
            prod *= f["effective_value"]
        self.assertAlmostEqual(prod, lead["lead_score"], places=3)

    def test_strength_matches_band(self):
        lead, bd = self._bd()
        if lead["lead_band"] in ("HIGH", "MEDIUM", "LOW"):
            self.assertEqual(bd["strength"], lead["lead_band"])


class Recency(unittest.TestCase):
    def test_buckets_by_age(self):
        today = dt.date(2026, 5, 29)
        # Dates safely inside each band (edges drift ~a day with leap years, which
        # is immaterial; we assert the band a clearly-aged permit lands in).
        self.assertEqual(recency_factor("2025-11-29", today), 1.0)   # ~0.5y
        self.assertEqual(recency_factor("2024-11-29", today), 0.8)   # ~1.5y
        self.assertEqual(recency_factor("2023-11-29", today), 0.55)  # ~2.5y
        self.assertEqual(recency_factor("2022-05-29", today), 0.3)   # ~4y
        self.assertEqual(recency_factor("2016-05-29", today), 0.1)   # ~10y

    def test_monotonic_non_increasing_with_age(self):
        today = dt.date(2026, 5, 29)
        ages = ["2026-05-01", "2024-11-29", "2023-11-29", "2022-05-29", "2010-01-01"]
        facs = [recency_factor(a, today) for a in ages]
        self.assertEqual(facs, sorted(facs, reverse=True))  # never increases with age

    def test_missing_or_bad_date_is_neutral(self):
        self.assertEqual(recency_factor(None), 1.0)
        self.assertEqual(recency_factor(""), 1.0)
        self.assertEqual(recency_factor("not-a-date"), 1.0)

    def test_future_date_treated_as_fresh(self):
        self.assertEqual(recency_factor("2027-01-01", dt.date(2026, 5, 29)), 1.0)

    def test_accepts_datetime_prefix(self):
        # apply_date in the DB is an ISO datetime; only the date head matters
        self.assertEqual(recency_factor("2004-04-01T00:00:00", dt.date(2026, 5, 29)), 0.1)


class CanonicalParcelByAddress(unittest.TestCase):
    """The step3b parcel-canonicalization guard: a NULL-parcel permit may adopt
    its address's parcel ONLY when every permit there agrees on ONE parcel."""

    def test_single_parcel_address_is_canonical(self):
        # Two permits at the same address, one parcel between them -> adoptable.
        m = canonical_parcel_by_address([
            ("123 MAIN", "050011210"), ("123 MAIN", None)])
        self.assertEqual(m, {"123 MAIN": "050011210"})

    def test_multi_parcel_address_is_excluded(self):
        # The hard-won guard: a multi-unit building with two distinct parcels
        # must NOT be canonicalized (adopting one would over-collapse unrelated
        # projects). The address is omitted from the map entirely.
        m = canonical_parcel_by_address([
            ("500 EL CAMINO", "050011210"), ("500 EL CAMINO", "050011211")])
        self.assertNotIn("500 EL CAMINO", m)

    def test_blank_address_or_parcel_ignored(self):
        # Empty/whitespace address or parcel contributes nothing.
        m = canonical_parcel_by_address([
            ("", "050011210"), ("  ", "X"), ("9 OAK", ""), ("9 OAK", None)])
        self.assertEqual(m, {})

    def test_repeated_same_parcel_still_single(self):
        # The same parcel seen many times is still ONE distinct parcel.
        m = canonical_parcel_by_address([
            ("7 PINE", "APN1"), ("7 PINE", "APN1"), ("7 PINE", None)])
        self.assertEqual(m, {"7 PINE": "APN1"})


class Clustering(unittest.TestCase):
    def test_key_precedence(self):
        self.assertEqual(cluster_key("050011210", "123 main", "CID"),
                         ("P:050011210", "PARCEL"))
        self.assertEqual(cluster_key(None, "123 main", "CID"),
                         ("A:123 main", "ADDRESS"))
        self.assertEqual(cluster_key("  ", "", "CID"), ("C:CID", "SINGLETON"))

    def test_address_unit_splits_multifamily(self):
        # NULL-parcel permits in a multifamily building must key PER UNIT, not by
        # the bare street (which over-collapsed 14 units of "1 LAUREL ST" into one
        # bogus project). A unit-less address (single-family norm) is unchanged.
        self.assertEqual(cluster_key(None, "1 laurel st", "C1", "201"),
                         ("A:1 laurel st|201", "ADDRESS"))
        self.assertEqual(cluster_key(None, "1 laurel st", "C2", "202"),
                         ("A:1 laurel st|202", "ADDRESS"))
        self.assertEqual(cluster_key(None, "123 main", "C3", None),
                         ("A:123 main", "ADDRESS"))
        self.assertEqual(cluster_key(None, "123 main", "C4", "  "),
                         ("A:123 main", "ADDRESS"))
        # A real parcel still wins over address+unit.
        self.assertEqual(cluster_key("050011210", "1 laurel st", "C5", "201"),
                         ("P:050011210", "PARCEL"))

    def test_lone_street_suffix_is_singleton(self):
        # 'DR', 'AVE', 'BLVD' etc. by themselves are EnerGov data-entry
        # leftovers, not real address keys. They must NOT cluster two
        # unrelated permits together via 'A:DR'.
        self.assertEqual(cluster_key(None, "DR", "C1"), ("C:C1", "SINGLETON"))
        self.assertEqual(cluster_key(None, "AVE", "C2"), ("C:C2", "SINGLETON"))
        self.assertEqual(cluster_key(None, "BLVD", "C3"), ("C:C3", "SINGLETON"))
        # Multi-word landmarks stay valid ADDRESS keys (real groupings).
        self.assertEqual(cluster_key(None, "HIGHLANDS PARK", "C4"),
                         ("A:HIGHLANDS PARK", "ADDRESS"))
        self.assertEqual(cluster_key(None, "CORNER ARROYO / EL CAMINO REAL", "C5"),
                         ("A:CORNER ARROYO / EL CAMINO REAL", "ADDRESS"))

    def test_aggregate_anchor_and_sums(self):
        members = [
            {"case_id": "a", "lead_score": 50.0, "lead_band": "HIGH",
             "category": "ADDITION", "valuation": 100000.0, "owner_name": "Owner A",
             "owner_email": None, "owner_phone": "1", "has_contractor": 0,
             "address_display": "1 A St", "main_parcel": "P1", "apply_date": "2025-01-01"},
            {"case_id": "b", "lead_score": 90.0, "lead_band": "HIGH",
             "category": "NEW_SFR", "valuation": 500000.0, "owner_name": "Owner B",
             "owner_email": "b@x.com", "owner_phone": None, "has_contractor": 1,
             "address_display": "1 A St", "main_parcel": "P1", "apply_date": "2025-03-01"},
        ]
        agg = aggregate("P:P1", "PARCEL", members)
        self.assertEqual(agg["primary_case_id"], "b")          # higher score anchors
        self.assertEqual(agg["max_lead_score"], 90.0)
        self.assertEqual(agg["permit_count"], 2)
        self.assertEqual(agg["total_valuation"], 600000.0)
        self.assertEqual(agg["has_contractor"], 1)             # any member
        self.assertEqual(agg["owner_email"], "b@x.com")        # most complete contact
        self.assertEqual(agg["categories"], "ADDITION, NEW_SFR")

    def test_anchor_wins_when_anchor_is_reachable(self):
        # The cluster's surfaced contact MUST be the anchor's (the highest-scoring
        # permit's) when the anchor's owner is reachable -- otherwise the GC's
        # call list shows a sub-trade contractor's number from a sibling permit
        # while the real homeowner of the lead project sits in the anchor row.
        # Real-world case (BLDR2026-00123): SFR anchor with owner SHARRON WILLIAM
        # was being overridden by an HVAC sub-trade permit's APPLICANT (Valley
        # Heating). With the anchor reachable, anchor wins.
        members = [
            {"case_id": "anchor", "lead_score": 0.9, "owner_name": "SFR Owner",
             "owner_email": "sfr@x.com", "owner_phone": None,
             "contact_role": "OWNER",
             "main_parcel": "P1", "apply_date": "2026-03-01"},
            {"case_id": "subtrade", "lead_score": 0.03, "owner_name": "HVAC Co",
             "owner_email": "hvac@x.com", "owner_phone": "555-1234567",
             "contact_role": "APPLICANT",
             "main_parcel": "P1", "apply_date": "2026-04-01"},
        ]
        agg = aggregate("P:P1", "PARCEL", members)
        self.assertEqual(agg["primary_case_id"], "anchor")
        self.assertEqual(agg["owner_name"], "SFR Owner")
        self.assertEqual(agg["contact_role"], "OWNER")

    def test_sibling_wins_when_anchor_contactless(self):
        # Only fall through to a sibling when the anchor has truly no contact
        # (no email AND no phone). This is the original case the fall-through
        # was designed for -- preserve it.
        members = [
            {"case_id": "anchor", "lead_score": 0.9, "owner_name": "Contactless",
             "owner_email": None, "owner_phone": None, "contact_role": "OWNER",
             "main_parcel": "P1", "apply_date": "2026-03-01"},
            {"case_id": "sibling", "lead_score": 0.3, "owner_name": "Reachable",
             "owner_email": "r@x.com", "owner_phone": "555-1234567",
             "contact_role": "APPLICANT",
             "main_parcel": "P1", "apply_date": "2026-04-01"},
        ]
        agg = aggregate("P:P1", "PARCEL", members)
        self.assertEqual(agg["primary_case_id"], "anchor")
        self.assertEqual(agg["owner_name"], "Reachable")
        self.assertEqual(agg["contact_role"], "APPLICANT")

    def test_aggregate_propagates_contact_role(self):
        # Cluster's contact_role must echo the role of the MEMBER whose contact
        # was chosen, so the deduped call list can label "Name (Architect)" the
        # same way the per-permit list does. Don't default to OWNER on a non-
        # owner contact (would mislabel the architect as the homeowner).
        members = [
            {"case_id": "a", "lead_score": 50.0, "owner_name": "Owner A",
             "owner_email": None, "owner_phone": None, "contact_role": "OWNER",
             "main_parcel": "P1", "apply_date": "2025-01-01"},
            {"case_id": "b", "lead_score": 0.3, "owner_name": "Architect B",
             "owner_email": "arc@x.com", "owner_phone": "555",
             "contact_role": "ARCHITECT",
             "main_parcel": "P1", "apply_date": "2025-03-01"},
        ]
        agg = aggregate("P:P1", "PARCEL", members)
        # Anchor is "a" (higher score) but contact wins on completeness ("b").
        self.assertEqual(agg["primary_case_id"], "a")
        self.assertEqual(agg["owner_email"], "arc@x.com")
        self.assertEqual(agg["contact_role"], "ARCHITECT")


class CustomFieldsAndHolds(unittest.TestCase):
    def test_fnum_coercion(self):
        self.assertIsNone(_fnum(0))
        self.assertIsNone(_fnum("0.0"))
        self.assertIsNone(_fnum("abc"))
        self.assertIsNone(_fnum(True))
        self.assertEqual(_fnum("863"), 863.0)
        self.assertEqual(_fnum("1,234"), 1234.0)
        self.assertEqual(_fnum(2.0), 2.0)

    def test_custom_fields_strip_trailing_space_labels(self):
        result = {"CustomFields": [
            {"Label": "Number of Stories ", "Value": 2.0},     # note trailing space
            {"Label": "Type of Construction ", "Value": "VB"},
            {"Label": "Empty", "Value": ""},                   # dropped
        ]}
        cf = _custom_fields(result)
        self.assertEqual(cf["number of stories"], 2.0)
        self.assertEqual(cf["type of construction"], "VB")
        self.assertNotIn("empty", cf)

    def test_holds_active_vs_blocking(self):
        result = {"Holds": [
            {"Active": True, "HoldTypeSetupName": "Expired Permit Hold"},  # not blocking
            {"Active": True, "HoldTypeSetupName": "Planning Hold"},        # blocking
            {"Active": False, "HoldTypeSetupName": "Stop Work Order"},     # inactive
        ]}
        self.assertEqual(_holds_summary(result), (2, 1))

    def test_parse_detail_integration(self):
        result = {
            "ValuationValue": 250000.0, "SquareFeet": 0.0,
            "CustomFields": [{"Label": "Additional Square Footage ", "Value": "863"}],
            "Holds": [{"Active": True, "HoldTypeSetupName": "Planning Hold"}],
            "Contacts": [{}], "Parcels": [], "Attachments": [{}],
            "Addresses": [{"Main": True, "ParcelNumber": "050011210"}],
        }
        d = parse_detail(result, "CID")
        self.assertEqual(d["valuation"], 250000.0)
        self.assertEqual(d["additional_sqft"], 863.0)
        self.assertEqual(d["main_parcel"], "050011210")
        self.assertEqual(d["active_hold_count"], 1)
        self.assertEqual(d["blocking_hold_count"], 1)

    def test_parse_detail_tolerates_wrong_typed_list_fields(self):
        # Schema drift / malformed 200: a list field returned as a truthy non-list
        # (string/int/dict) must degrade to 'no rows / 0 count', not crash the
        # whole step2 parse (parse_detail isn't wrapped per-record).
        result = {"ValuationValue": 100.0, "Contacts": "notalist",
                  "CustomFields": 7, "Holds": {"x": 1}, "Parcels": "p",
                  "Attachments": 3, "Addresses": "a"}
        d = parse_detail(result, "CID")
        self.assertEqual(d["valuation"], 100.0)   # scalar still read
        self.assertEqual((d["contact_count"], d["hold_count"],
                          d["parcel_count"], d["attachment_count"]), (0, 0, 0, 0))
        self.assertIsNone(d["main_parcel"])
        self.assertIsNone(d["additional_sqft"])
        self.assertEqual(parse_contacts(result, "CID"), [])


class AddressNormalization(unittest.TestCase):
    def test_strips_city_tail(self):
        self.assertEqual(split_address("825 INDUSTRIAL RD SAN CARLOS CA 94070"),
                         ("825 INDUSTRIAL RD", ""))
        self.assertEqual(
            split_address("2017 GREENWOOD AVE, SAN CARLOS, CA 94070-1234"),
            ("2017 GREENWOOD AVE", ""))

    def test_separates_unit(self):  # multifamily must not collapse into the base
        self.assertEqual(
            split_address("1460 ALAMEDA, Apt 29, San Carlos CA 94070"),
            ("1460 ALAMEDA", "APT 29"))

    def test_blank_and_trailing_star(self):
        self.assertEqual(split_address(None), ("", ""))
        self.assertEqual(split_address("   "), ("", ""))
        # A trailing "*" (an Accela-era flag; absent from SC data) is stripped.
        self.assertEqual(split_address("123 MAIN ST *"), ("123 MAIN ST", ""))

    def test_energov_unit_separator(self):
        # EnerGov's primary multifamily address format uses "Unit:" with no
        # comma — found 3,594 of 3,616 such records were missing extraction
        # before the fix. The base address must clean up (no leaked "UNIT:")
        # and the unit must populate.
        self.assertEqual(
            split_address("1500 LAUREL ST Unit: SUITE B"),
            ("1500 LAUREL ST", "SUITE B"))
        self.assertEqual(
            split_address("1263 CHERRY ST Unit: APT # 304 SAN CARLOS CA 94070"),
            ("1263 CHERRY ST", "APT # 304"))
        self.assertEqual(
            split_address("907 E. SAN CARLOS AVE Unit: UNIT 6"),
            ("907 E. SAN CARLOS AVE", "UNIT 6"))
        self.assertEqual(
            split_address("1000 COMMERCIAL ST Unit: UNIT C"),
            ("1000 COMMERCIAL ST", "UNIT C"))

    def test_preserves_marker_in_unit(self):
        # The marker (APT/SUITE/UNIT/#) is canonical context for the unit -- it
        # qualifies how the unit number is referred to and should stay attached.
        # Previously the regex stripped it, so "APT 29" became just "29".
        self.assertEqual(
            split_address("648 WALNUT ST Unit: APT #2"),
            ("648 WALNUT ST", "APT #2"))
        self.assertEqual(
            split_address("25 DEVONSHIRE BLVD Unit: APT. 2"),
            ("25 DEVONSHIRE BLVD", "APT. 2"))


class Step1CorruptPage(unittest.TestCase):
    """A corrupt/truncated cached page must NOT abort step1: load_json only
    defaults on a MISSING file, so a malformed-but-present page raises
    JSONDecodeError -- and step1 is the tick's CRITICAL step, so an unhandled
    error there aborts the whole tick. _parse_pages skips the bad page and parses
    the rest, mirroring step0's defensive handling."""

    def _page(self, d, name, text):
        (Path(d) / name).write_text(text, encoding="utf-8")
        return Path(d) / name

    def test_corrupt_page_skipped_good_pages_still_parsed(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            good = self._page(d, "page_001.json", json.dumps(
                {"EntityResults": [{"CaseId": "a", "CaseNumber": "1"},
                                   {"CaseId": "b", "CaseNumber": "2"}]}))
            bad = self._page(d, "page_002.json", "{ truncated json …")
            good2 = self._page(d, "page_003.json", json.dumps(
                {"EntityResults": [{"CaseId": "c", "CaseNumber": "3"}]}))
            rows, bad_names = _parse_pages([good, bad, good2], "Permit",
                                           log=lambda *_: None)
            self.assertEqual({r["case_id"] for r in rows}, {"a", "b", "c"})
            self.assertEqual(bad_names, ["page_002.json"])  # the corrupt one only

    def test_all_pages_good_reports_no_bad(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            p = self._page(d, "page_001.json",
                           json.dumps({"EntityResults": [{"CaseId": "a"}]}))
            rows, bad_names = _parse_pages([p], "Permit", log=lambda *_: None)
            self.assertEqual(bad_names, [])
            self.assertEqual(len(rows), 1)


class Step1Parsing(unittest.TestCase):
    def test_parse_page_skips_entities_without_caseid(self):
        result = {"EntityResults": [
            {"CaseId": "G1", "CaseNumber": "BLDR2025-1",
             "AddressDisplay": "1 A ST SAN CARLOS CA 94070"},
            {"CaseNumber": "NO-ID"},   # missing CaseId -> dropped (it's the PK)
        ]}
        rows = parse_page(result, "Permit", 3)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["case_id"], "G1")
        self.assertEqual(rows[0]["address_norm"], "1 A ST")
        self.assertEqual(rows[0]["source_page"], 3)

    def test_map_entity_drops_nested_address_object(self):
        # The structured `Address` object must not leak into a scalar column.
        row = map_entity({"CaseId": "G2", "CaseNumber": "X",
                          "AddressDisplay": "5 B AVE SAN CARLOS CA 94070",
                          "Address": {"City": "SAN CARLOS"}}, "Permit", 1)
        self.assertEqual(row["address_display"], "5 B AVE SAN CARLOS CA 94070")
        self.assertEqual(row["address_norm"], "5 B AVE")

    def test_parse_page_tolerates_wrong_typed_entityresults(self):
        # Schema drift / malformed 200: EntityResults a non-list must degrade to
        # 'no rows', not crash map_entity's .get() and poison the parse.
        for bad in ("notalist", 42, {"a": 1}, None):
            self.assertEqual(parse_page({"EntityResults": bad}, "Permit", 1), [])


class SqlLiteral(unittest.TestCase):
    def test_lit_escaping(self):
        self.assertEqual(_lit(None), "NULL")
        self.assertEqual(_lit(True), "1")
        self.assertEqual(_lit(False), "0")
        self.assertEqual(_lit(5), "5")
        self.assertEqual(_lit(5.5), "5.5")
        self.assertEqual(_lit("plain"), "'plain'")
        self.assertEqual(_lit("it's a 10'-0\" deck"), "'it''s a 10''-0\" deck'")

    def test_lit_collapses_control_chars(self):
        # EnerGov descriptions sometimes embed literal newlines (80 actionable
        # rows on 2026-05-29), breaking one-line-per-row SQL formatting and
        # risking strict-parser breakage. Collapse newline / tab / CR / other
        # ASCII control chars to a single space; preserve unicode.
        self.assertEqual(_lit("line1\nline2"), "'line1 line2'")
        self.assertEqual(_lit("col1\tcol2"), "'col1 col2'")
        self.assertEqual(_lit("a\r\nb"), "'a  b'")
        self.assertEqual(_lit("café"), "'café'")  # unicode survives

    def test_lit_nan_and_inf_become_null(self):
        # repr(nan)/repr(inf) emit bare 'nan'/'inf' tokens that break the whole
        # INSERT batch on SQLite/D1. They have no SQL literal -> NULL them.
        self.assertEqual(_lit(float("nan")), "NULL")
        self.assertEqual(_lit(float("inf")), "NULL")
        self.assertEqual(_lit(float("-inf")), "NULL")
        self.assertEqual(_lit(0.0), "0.0")   # ordinary floats unaffected


class CsvCallSheet(unittest.TestCase):
    """--csv writes a curated call sheet: the same actionable rows, mapped from
    the SELECT's column order to the call-friendly csv_columns by name."""

    def test_csv_columns_reference_real_export_columns(self):
        # Every csv source column must exist in the spec's export columns, else
        # the name->index map would KeyError at runtime.
        for spec in (LEADS_SPEC, CLUSTERS_SPEC):
            export_cols = {name for name, _ in spec["columns"]}
            for src, _header in spec["csv_columns"]:
                self.assertIn(src, export_cols,
                              f"{spec['name']}: csv col {src!r} not in export")

    def test_csv_rows_maps_values_by_source_column(self):
        # Build a fake fetched row in CLUSTERS_SPEC['columns'] order and confirm
        # _csv_rows pulls each curated field from the right position.
        spec = CLUSTERS_SPEC
        col_order = [name for name, _ in spec["columns"]]
        row = tuple(f"<{name}>" for name in col_order)  # sentinel per column
        headers, dicts = _csv_rows(spec, [row])
        self.assertEqual(headers,
                         [h for _, h in spec["csv_columns"]])
        # e.g. the 'address' header must carry the address_display value
        self.assertEqual(dicts[0]["address"], "<address_display>")
        self.assertEqual(dicts[0]["outreach_contact"], "<owner_name>")
        self.assertEqual(dicts[0]["property_owner"], "<property_owner_name>")
        self.assertEqual(dicts[0]["phone"], "<owner_phone>")
        self.assertEqual(dicts[0]["band"], "<top_band>")

    def test_csv_rows_empty_export(self):
        headers, dicts = _csv_rows(CLUSTERS_SPEC, [])
        self.assertEqual(dicts, [])
        self.assertTrue(headers)  # headers always present (for an empty sheet)


class PruneStatement(unittest.TestCase):
    SPEC = {"table": "sca_leads", "columns": [("case_id", "TEXT PRIMARY KEY"),
                                              ("lead_score", "REAL")]}

    def test_prune_keeps_only_exported_pks(self):
        rows = [("a-1", 90.0), ("b-2", 40.0)]
        sql = _prune_statement(self.SPEC, rows)
        # mirror-delete scoped to the table, keyed on the PK, escaping each id
        self.assertEqual(
            sql, "DELETE FROM sca_leads WHERE case_id NOT IN ('a-1', 'b-2');")

    def test_prune_escapes_quotes_in_ids(self):
        sql = _prune_statement(self.SPEC, [("o'brien", 1.0)])
        self.assertIn("'o''brien'", sql)

    def test_prune_empty_export_clears_table(self):
        # an empty export mirrors to an empty table (no NOT IN with no values)
        self.assertEqual(_prune_statement(self.SPEC, []),
                         "DELETE FROM sca_leads;")


class ExportSinceFloor(unittest.TestCase):
    """The --since export floor: pre-2020 'Approved' zombies (recency_factor 0.1
    isn't quite enough to push them out of band LOW) get filtered at export.
    Matches the tick's --backfill-since horizon so we don't enrich what we don't
    publish. NULL apply_date stays IN-scope (don't silently lose live records
    with missing apply_date due to a portal quirk)."""

    def _db(self):
        c = sqlite3.connect(":memory:")
        self.addCleanup(c.close)
        c.executescript("""
            CREATE TABLE sca_permits (case_id TEXT PRIMARY KEY, case_number TEXT,
                case_status TEXT, address_display TEXT, main_parcel TEXT,
                apply_date TEXT, issue_date TEXT, description TEXT);
            CREATE TABLE sca_leads (case_id TEXT PRIMARY KEY, lead_score REAL,
                lead_band TEXT, category TEXT, status_bucket TEXT, valuation REAL,
                additional_sqft REAL, num_stories REAL, construction_type TEXT,
                blocking_hold INTEGER, has_contractor INTEGER, owner_name TEXT,
                owner_email TEXT, owner_phone TEXT, contact_role TEXT,
                property_owner_name TEXT, contractor_name TEXT,
                scored_at TEXT, cluster_id TEXT, cluster_key_type TEXT,
                score_breakdown TEXT);
            CREATE TABLE sca_lead_clusters (cluster_id TEXT PRIMARY KEY,
                key_type TEXT, permit_count INTEGER, max_lead_score REAL,
                top_band TEXT, categories TEXT, total_valuation REAL,
                max_valuation REAL, primary_case_id TEXT, address_display TEXT,
                main_parcel TEXT, owner_name TEXT, owner_email TEXT,
                owner_phone TEXT, contact_role TEXT, property_owner_name TEXT,
                has_contractor INTEGER,
                first_apply_date TEXT, last_apply_date TEXT);
        """)
        # 3 permits: pre-cutoff zombie, post-cutoff live, NULL-date oddball.
        c.executemany("INSERT INTO sca_permits VALUES (?,?,?,?,?,?,?,?)", [
            ("z-old", "BLD2008-1", "Approved", "1 OLD ST", "p1",
             "2008-06-11T00:00:00", "2008-10-03", "old"),
            ("y-new", "BLD2025-1", "Issued", "2 NEW ST", "p2",
             "2025-03-01T00:00:00", None, "new"),
            ("x-null", "BLD2024-1", "In Review", "3 X ST", "p3",
             None, None, "nulldate"),
        ])
        c.executemany(
            "INSERT INTO sca_leads (case_id, lead_score, lead_band) VALUES (?,?,?)",
            [("z-old", 8.0, "LOW"), ("y-new", 60.0, "HIGH"), ("x-null", 30.0, "MEDIUM")])
        c.commit()
        return c

    def test_leads_since_floor_drops_pre_cutoff(self):
        c = self._db()
        rows = _fetch_rows(c, LEADS_SPEC, ["HIGH", "MEDIUM", "LOW"], "2020-01-01", None)
        case_ids = {r[0] for r in rows}
        self.assertEqual(case_ids, {"y-new", "x-null"})  # z-old excluded; NULL stays

    def test_leads_no_since_includes_all_bands(self):
        c = self._db()
        rows = _fetch_rows(c, LEADS_SPEC, ["HIGH", "MEDIUM", "LOW"], None, None)
        self.assertEqual({r[0] for r in rows}, {"z-old", "y-new", "x-null"})

    def test_clusters_since_floor_uses_last_apply_date(self):
        c = self._db()
        c.executemany("INSERT INTO sca_lead_clusters (cluster_id, top_band, "
                      "first_apply_date, last_apply_date) VALUES (?,?,?,?)", [
            ("c-zombie", "LOW", "2008-06-11", "2008-10-03"),       # all pre-cutoff
            ("c-mixed",  "HIGH", "2008-06-11", "2024-05-01"),       # newest in-scope
            ("c-null",   "MEDIUM", None, None),                     # NULL stays
        ])
        c.commit()
        rows = _fetch_rows(c, CLUSTERS_SPEC, ["HIGH", "MEDIUM", "LOW"], "2020-01-01", None)
        ids = {r[0] for r in rows}
        self.assertEqual(ids, {"c-mixed", "c-null"})  # c-zombie excluded


class MissingDetailSelection(unittest.TestCase):
    """The historical-backfill selector: --missing-detail picks only permits with
    no parsed detail row, newest-first, respecting the chunk limit."""

    def _db(self):
        c = sqlite3.connect(":memory:")
        self.addCleanup(c.close)
        c.executescript("""
            CREATE TABLE sca_permits (case_id TEXT PRIMARY KEY, module TEXT,
                apply_date TEXT, case_status TEXT, case_type TEXT);
            CREATE TABLE sca_permit_detail (case_id TEXT PRIMARY KEY);
        """)
        c.executemany("INSERT INTO sca_permits VALUES (?,?,?,?,?)", [
            ("p-new", "Permit", "2026-03-01T00:00:00", "In Review", "X"),
            ("p-mid", "Permit", "2025-06-01T00:00:00", "Issued", "X"),
            ("p-old", "Permit", "2024-01-01T00:00:00", "Complete", "X"),
        ])
        c.execute("INSERT INTO sca_permit_detail (case_id) VALUES ('p-mid')")
        c.commit()
        return c

    def test_excludes_already_detailed_newest_first(self):
        c = self._db()
        got = select_case_ids(c, "Permit", None, None, None, None, None, None,
                              missing_detail=True)
        self.assertEqual(got, ["p-new", "p-old"])  # p-mid has detail -> excluded

    def test_respects_limit(self):
        c = self._db()
        got = select_case_ids(c, "Permit", None, None, None, None, None, 1,
                              missing_detail=True)
        self.assertEqual(got, ["p-new"])  # newest missing wins the single slot

    def test_since_floor_skips_pre_cutoff_missing(self):
        # The --backfill-since floor: a pre-2020 missing record is excluded even
        # though it lacks detail (the tick's default skips the archival tail).
        c = self._db()
        c.execute("INSERT INTO sca_permits VALUES "
                  "('p-1999','Permit','1999-05-01T00:00:00','Approved','X')")
        c.commit()
        got = select_case_ids(c, "Permit", None, None, "2020-01-01", None, None,
                              None, missing_detail=True)
        self.assertEqual(got, ["p-new", "p-old"])  # p-1999 below floor -> excluded


class _FakeResp:
    def __init__(self, status, payload=None, text=""):
        self.status_code = status
        self._payload = payload if payload is not None else {}
        self.text = text

    def json(self):
        return self._payload


class _BadJsonResp:
    """A 200 response whose body isn't valid JSON: .json() raises ValueError,
    mirroring requests' JSONDecodeError (a ValueError subclass)."""
    status_code = 200
    text = "<html>error</html>"

    def json(self):
        raise ValueError("Expecting value: line 1 column 1 (char 0)")


class _FakeSession:
    """Returns a scripted sequence of responses, one per .get() call."""
    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = 0

    def get(self, url, timeout=None):
        self.calls += 1
        return self._responses.pop(0)


class DetailFetchRetry(unittest.TestCase):
    """fetch_one rides through a TRANSIENT 401 (anonymous endpoint, server blip)
    via backoff, but still gives up on a genuinely non-retryable status."""

    def setUp(self):
        self._sleep = detailmod.time.sleep
        detailmod.time.sleep = lambda *_: None      # no real backoff in tests

    def tearDown(self):
        detailmod.time.sleep = self._sleep

    def test_transient_401_then_200_succeeds(self):
        sess = _FakeSession([
            _FakeResp(401, text="Authorization has been denied"),
            _FakeResp(200, {"Success": True, "Result": {"ok": 1}}),
        ])
        result = detailmod.fetch_one(sess, "case-x")
        self.assertEqual(result, {"ok": 1})
        self.assertEqual(sess.calls, 2)             # retried once, then succeeded

    def test_404_is_not_retried(self):
        sess = _FakeSession([_FakeResp(404, text="not found")])
        with self.assertRaises(detailmod.DetailError):
            detailmod.fetch_one(sess, "case-y")
        self.assertEqual(sess.calls, 1)             # hard error -> no retry

    def test_persistent_401_eventually_raises(self):
        sess = _FakeSession([_FakeResp(401, text="denied")
                             for _ in range(detailmod.MAX_RETRIES + 1)])
        with self.assertRaises(detailmod.DetailError):
            detailmod.fetch_one(sess, "case-z")
        self.assertEqual(sess.calls, detailmod.MAX_RETRIES + 1)  # bounded, no infinite loop

    def test_200_with_unparseable_body_is_retried(self):
        # A 200 whose body isn't JSON (truncated / HTML error page from the
        # flaky IIS host) is a transient blip -> retry, not a crash. r.json()
        # lives in the try/except's else-clause, so a ValueError here would
        # otherwise escape fetch_one uncaught and kill the whole backfill.
        sess = _FakeSession([
            _BadJsonResp(),
            _FakeResp(200, {"Success": True, "Result": {"ok": 2}}),
        ])
        result = detailmod.fetch_one(sess, "case-bad")
        self.assertEqual(result, {"ok": 2})
        self.assertEqual(sess.calls, 2)

    def test_persistent_unparseable_200_raises_detailerror(self):
        # After exhausting retries it must raise DetailError (which
        # fetch_details catches + skips), NOT a bare ValueError (which it
        # doesn't catch -> backfill crash). Bounded, no infinite loop.
        sess = _FakeSession([_BadJsonResp()
                             for _ in range(detailmod.MAX_RETRIES + 1)])
        with self.assertRaises(detailmod.DetailError):
            detailmod.fetch_one(sess, "case-bad2")
        self.assertEqual(sess.calls, detailmod.MAX_RETRIES + 1)


class ReadCachedDetail(unittest.TestCase):
    """The detail cache is cache-SKIPPED (never auto-overwritten), so a corrupt
    cached detail file would be a poison pill: it crashes the parse and persists.
    _read_cached_detail must (a) parse a good file, (b) REMOVE a corrupt-content
    file so the next fetch re-fetches it (self-healing), and (c) NOT crash."""

    def test_good_file_parsed(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "abc.json"
            f.write_text(json.dumps({"ValuationValue": 1000.0}))
            self.assertEqual(_read_cached_detail(f, log=lambda *_: None),
                             {"ValuationValue": 1000.0})
            self.assertTrue(f.exists())  # good file is left in place

    def test_corrupt_file_removed_and_returns_none(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "bad.json"
            f.write_text("{ truncated …")
            self.assertIsNone(_read_cached_detail(f, log=lambda *_: None))
            # Removed so the next step2 fetch re-fetches a clean copy.
            self.assertFalse(f.exists())

    def test_missing_file_returns_empty_dict_not_none(self):
        # A missing file isn't an error here (load_json defaults); only present-
        # but-unreadable files are the poison-pill case.
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "gone.json"
            self.assertEqual(_read_cached_detail(f, log=lambda *_: None), {})


class UnparsedFiles(unittest.TestCase):
    """--missing-only parse: keep only cached files whose case_id has no detail
    row yet, preserving order — so a tick parses just the freshly-fetched chunk."""

    def _files(self, *stems):
        return [Path(f"/cache/{s}.json") for s in stems]

    def test_skips_already_parsed(self):
        files = self._files("p-new", "p-mid", "p-old")
        got = unparsed_files(files, {"p-mid"})
        self.assertEqual([f.stem for f in got], ["p-new", "p-old"])

    def test_empty_when_all_parsed(self):
        files = self._files("a", "b")
        self.assertEqual(unparsed_files(files, {"a", "b"}), [])

    def test_all_when_none_parsed(self):
        files = self._files("a", "b")
        self.assertEqual(unparsed_files(files, set()), files)


class TickCadence(unittest.TestCase):
    """The two-cadence gate: when to do the heavy search re-pull vs. skip. This
    is the pipeline's one portal-politeness decision, so it gets locked down."""

    NOW = dt.datetime(2026, 5, 28, 12, 0, tzinfo=dt.timezone.utc)

    def test_refresh_when_no_prior_run(self):
        self.assertTrue(should_refresh(False, None, self.NOW, 6.0))

    def test_refresh_when_forced_even_if_recent(self):
        recent = self.NOW - dt.timedelta(hours=1)
        self.assertTrue(should_refresh(True, recent, self.NOW, 6.0))

    def test_no_refresh_within_throttle_window(self):
        recent = self.NOW - dt.timedelta(hours=2)
        self.assertFalse(should_refresh(False, recent, self.NOW, 6.0))

    def test_refresh_once_window_elapsed(self):
        old = self.NOW - dt.timedelta(hours=6, minutes=1)
        self.assertTrue(should_refresh(False, old, self.NOW, 6.0))

    def test_skip_entirely_only_when_throttled_and_backfill_done(self):
        # throttled + nothing missing -> skip
        self.assertTrue(should_skip_entirely(False, 200, 0))
        # throttled + backfill disabled -> skip
        self.assertTrue(should_skip_entirely(False, 0, 999))
        # throttled but records still missing -> backfill-only pass, don't skip
        self.assertFalse(should_skip_entirely(False, 200, 999))
        # refreshing -> never skip, regardless of backfill state
        self.assertFalse(should_skip_entirely(True, 0, 0))

    def test_outage_backoff_skips_refresh_after_recent_failure(self):
        # After a refresh fails (portal 500), the next fire should NOT retry --
        # each attempt costs ~3 min of retry timeouts. Even if the normal
        # throttle window has elapsed, the recent failure wins.
        last_success = self.NOW - dt.timedelta(hours=8)   # throttle elapsed
        last_failure = self.NOW - dt.timedelta(minutes=5)  # recent failure
        self.assertFalse(should_refresh(False, last_success, self.NOW, 6.0,
                                        last_failure=last_failure))

    def test_outage_backoff_clears_after_window_passes(self):
        # Once the outage-backoff window has elapsed (30 min default), the
        # normal throttle check takes over. With a normal throttle elapsed,
        # the refresh runs again.
        last_success = self.NOW - dt.timedelta(hours=8)
        last_failure = self.NOW - dt.timedelta(minutes=45)  # > 30 min
        self.assertTrue(should_refresh(False, last_success, self.NOW, 6.0,
                                       last_failure=last_failure))

    def test_force_overrides_outage_backoff(self):
        # An operator using --force always wins over backoff -- they're
        # explicitly asking to retry.
        last_failure = self.NOW - dt.timedelta(minutes=2)
        self.assertTrue(should_refresh(True, None, self.NOW, 6.0,
                                       last_failure=last_failure))

    def test_no_outage_backoff_when_no_recent_failure(self):
        # With no failure on record, the function behaves exactly like before
        # (no regression on the existing throttle logic).
        recent_success = self.NOW - dt.timedelta(hours=2)
        self.assertFalse(should_refresh(False, recent_success, self.NOW, 6.0,
                                        last_failure=None))


class TestEscalatedBackoff(unittest.TestCase):
    """The outage backoff widens with consecutive failures so a sustained portal
    block (the 403 can last hours) is re-poked far less often than a one-off."""

    def test_first_failure_uses_base_window(self):
        # 0 or 1 consecutive failures -> the original flat 30-min window, so a
        # single blip behaves exactly as before (no regression).
        self.assertEqual(escalated_backoff_minutes(0), OUTAGE_BACKOFF_MINUTES)
        self.assertEqual(escalated_backoff_minutes(1), OUTAGE_BACKOFF_MINUTES)

    def test_window_doubles_each_consecutive_failure(self):
        self.assertEqual(escalated_backoff_minutes(2), 60)
        self.assertEqual(escalated_backoff_minutes(3), 120)
        self.assertEqual(escalated_backoff_minutes(4), 240)

    def test_window_is_capped(self):
        # Beyond the cap the wait stops growing (never wait absurdly long, and
        # never overflow into multi-day silence).
        self.assertEqual(escalated_backoff_minutes(5), MAX_OUTAGE_BACKOFF_MINUTES)
        self.assertEqual(escalated_backoff_minutes(99), MAX_OUTAGE_BACKOFF_MINUTES)

    def test_negative_or_garbage_count_is_safe(self):
        # A corrupt/negative counter must not crash or produce a sub-base wait.
        self.assertEqual(escalated_backoff_minutes(-3), OUTAGE_BACKOFF_MINUTES)

    def test_escalated_window_keeps_refresh_skipped_longer(self):
        # End-to-end: after 3 consecutive failures the effective window is 2h, so
        # a failure 45 min ago (which a flat 30-min backoff would have cleared)
        # still suppresses the refresh.
        last_success = self.NOW - dt.timedelta(hours=8)
        last_failure = self.NOW - dt.timedelta(minutes=45)
        eff = escalated_backoff_minutes(3)   # 120 min
        self.assertFalse(should_refresh(False, last_success, self.NOW, 6.0,
                                        last_failure=last_failure,
                                        outage_backoff_minutes=eff))

    NOW = dt.datetime(2026, 6, 1, 12, 0, 0,
                      tzinfo=dt.timezone(dt.timedelta(hours=-4)))


class ConsecutiveFailureCounter(unittest.TestCase):
    """The consecutive_failures counter that drives the escalating backoff:
    _record_failure increments, _record_success resets, corrupt priors coerce
    to 0. State I/O is real (temp file), so this locks in the whole cycle."""

    def _isolate_state(self):
        import tempfile, tick
        d = tempfile.mkdtemp()
        self.addCleanup(__import__("shutil").rmtree, d, True)
        orig = tick.STATE_PATH
        tick.STATE_PATH = Path(d) / "state.json"
        self.addCleanup(setattr, tick, "STATE_PATH", orig)
        return tick

    def test_failure_increments_then_success_resets(self):
        tick = self._isolate_state()
        self.assertEqual(tick._consecutive_failures(), 0)        # fresh
        tick._record_failure(); self.assertEqual(tick._consecutive_failures(), 1)
        tick._record_failure(); self.assertEqual(tick._consecutive_failures(), 2)
        tick._record_failure(); self.assertEqual(tick._consecutive_failures(), 3)
        tick._record_success()
        self.assertEqual(tick._consecutive_failures(), 0)        # cleared
        # ...and a failure after a success starts the curve over at 1.
        tick._record_failure(); self.assertEqual(tick._consecutive_failures(), 1)

    def test_corrupt_counter_coerces_to_zero(self):
        tick = self._isolate_state()
        from utils.io import atomic_write_json
        for bad in ("notanint", -5, None, 2.5):
            atomic_write_json(tick.STATE_PATH, {"consecutive_failures": bad})
            self.assertEqual(tick._consecutive_failures(), 0)
            tick._record_failure()   # corrupt prior -> treated as 0 -> 1
            self.assertEqual(tick._consecutive_failures(), 1)


class _FakePostSession:
    """Answers the count query (PageSize 1) with a fixed TotalFound; never
    needs to serve a page because the test pre-populates the cache."""
    def __init__(self, total):
        self.total = total
        self.posts = 0

    def post(self, url, json=None, timeout=None):
        self.posts += 1
        return _FakeResp(200, {"Success": True,
                               "Result": {"TotalFound": self.total,
                                          "EntityResults": []}})


class StaleRefresh(unittest.TestCase):
    """Step 0b re-pulls the ApplyDate day-windows of actionable leads filed before
    the refresh floor, so their status doesn't go stale outside the main window."""

    def test_refresh_floor_matches_tick_window(self):
        # refresh_years=2, 2026 -> 2025-01-01 (2025+2026 refreshed; <2025 isn't).
        self.assertEqual(refresh_floor(dt.date(2026, 6, 1), 2), "2025-01-01")
        self.assertEqual(refresh_floor(dt.date(2026, 6, 1), 4), "2023-01-01")

    def test_stale_apply_days_dedups_and_sorts(self):
        # Many leads filed the same day cost ONE day-window; output sorted dates.
        days = stale_apply_days([
            "2024-05-15T10:00:00", "2024-05-15T14:00:00",  # same day -> one
            "2024-01-02T00:00:00", "2023-11-30T09:00:00"])
        self.assertEqual(days, [dt.date(2023, 11, 30), dt.date(2024, 1, 2),
                                dt.date(2024, 5, 15)])

    def test_stale_apply_days_skips_blank_and_unparseable(self):
        days = stale_apply_days([None, "", "not-a-date", "2024-03-04T00:00:00"])
        self.assertEqual(days, [dt.date(2024, 3, 4)])

    def test_at_risk_query_selects_old_actionable_only(self):
        c = sqlite3.connect(":memory:")
        self.addCleanup(c.close)
        c.executescript("""
            CREATE TABLE sca_permits (case_id TEXT PRIMARY KEY, apply_date TEXT);
            CREATE TABLE sca_leads (case_id TEXT PRIMARY KEY, lead_band TEXT,
                status_bucket TEXT);
        """)
        c.executemany("INSERT INTO sca_permits VALUES (?,?)", [
            ("old-hi", "2024-06-01T00:00:00"),   # old + HIGH -> at risk
            ("old-drop", "2024-06-01T00:00:00"),  # old but DROP -> not actionable
            ("new-hi", "2025-06-01T00:00:00"),    # HIGH but in-window -> skip
            ("null-hi", None),                    # no apply_date -> skip
        ])
        c.executemany("INSERT INTO sca_leads VALUES (?,?,?)", [
            ("old-hi", "HIGH", "READY_TO_ISSUE"), ("old-drop", "DROP", "DEAD"),
            ("new-hi", "HIGH", "IN_REVIEW"), ("null-hi", "MEDIUM", "IN_REVIEW")])
        c.commit()
        got = _at_risk_apply_dates(c, "2025-01-01")
        self.assertEqual(got, ["2024-06-01T00:00:00"])  # only old-hi

    def test_at_risk_query_tolerates_missing_tables(self):
        # Brand-new DB (no sca_leads yet) -> [] not a crash.
        c = sqlite3.connect(":memory:")
        self.addCleanup(c.close)
        self.assertEqual(_at_risk_apply_dates(c, "2025-01-01"), [])

    def test_day_loop_all_succeed(self):
        days = [dt.date(2024, 1, i) for i in range(1, 6)]
        fetched = []
        out = fetch_day_windows(days, fetched.append, log=lambda *_: None,
                                sleep=lambda *_: None)
        self.assertEqual(out["attempted"], 5)
        self.assertFalse(out["aborted"])
        self.assertEqual(len(fetched), 5)

    def test_day_loop_aborts_after_consecutive_portal_errors(self):
        # The portal rate-limits (real 2026-06-01 403): once it starts blocking,
        # stop hammering -- don't grind through all remaining day-windows.
        days = [dt.date(2024, 1, i) for i in range(1, 21)]
        attempts = []

        def always_403(d):
            attempts.append(d)
            raise SearchError("HTTP 403")
        out = fetch_day_windows(days, always_403, log=lambda *_: None,
                                sleep=lambda *_: None)
        self.assertTrue(out["aborted"])
        self.assertEqual(out["attempted"], MAX_CONSECUTIVE_ERRORS)  # stopped early
        self.assertEqual(len(attempts), MAX_CONSECUTIVE_ERRORS)     # not all 20

    def test_day_loop_resets_run_on_intermittent_error(self):
        # A single failure between successes must NOT abort -- only a CONSECUTIVE
        # run of errors means an active block.
        days = [dt.date(2024, 1, i) for i in range(1, 8)]
        seen = []

        def fail_on_third(d):
            seen.append(d)
            if d.day == 3:
                raise SearchError("HTTP 500 blip")
        out = fetch_day_windows(days, fail_on_third, log=lambda *_: None,
                                sleep=lambda *_: None)
        self.assertFalse(out["aborted"])
        self.assertEqual(out["attempted"], 7)        # all attempted
        self.assertEqual(len(out["errors"]), 1)


class LockReclaim(unittest.TestCase):
    """The run-lock must reclaim a stale lock without ever running two ticks at
    once. The hard case is pid RECYCLING: a SIGKILL'd tick (lock never released)
    whose pid the OS reuses for an unrelated LIVE process would otherwise wedge
    the pipeline forever, since every fire sees a 'live' holder and skips."""

    def test_live_recent_holder_is_not_reclaimed(self):
        # A genuinely running tick: live pid, lock a minute old -> don't reclaim.
        self.assertFalse(_should_reclaim_lock(1234, alive=True, age_seconds=60))

    def test_dead_holder_is_reclaimed(self):
        self.assertTrue(_should_reclaim_lock(1234, alive=False, age_seconds=5))

    def test_unknown_holder_is_reclaimed(self):
        # Unreadable/garbage lock body -> holder -1 -> reclaim regardless of age.
        self.assertTrue(_should_reclaim_lock(-1, alive=False, age_seconds=None))

    def test_live_but_stale_holder_is_reclaimed_pid_recycle_guard(self):
        # The pid-recycle guard: a 'live' pid on an hours-old lock can't be a real
        # tick (those finish in minutes), so it's a recycled pid -> reclaim.
        self.assertTrue(_should_reclaim_lock(
            1234, alive=True, age_seconds=LOCK_STALE_SECONDS + 1))

    def test_live_holder_just_under_stale_bound_is_kept(self):
        self.assertFalse(_should_reclaim_lock(
            1234, alive=True, age_seconds=LOCK_STALE_SECONDS - 1))

    def test_missing_age_with_live_holder_is_kept(self):
        # stat() failed (age None) but pid is live and readable -> conservative:
        # treat as a running tick, don't reclaim (avoid racing a live holder).
        self.assertFalse(_should_reclaim_lock(1234, alive=True, age_seconds=None))


class RefreshStateAttribution(unittest.TestCase):
    """The refresh state (throttle clock + outage backoff) must be keyed on the
    NETWORK pull (step0) only, never on the overall pipeline rc. A local step3
    scoring failure must NOT record a portal outage, and a successful pull must
    reset the throttle even if a later local step failed."""

    def test_successful_pull_records_success(self):
        self.assertEqual(refresh_state_action(True), "record_success")

    def test_failed_pull_records_failure(self):
        self.assertEqual(refresh_state_action(False), "record_failure")

    def test_no_refresh_records_nothing(self):
        # Backfill-only / throttled fire: step0 never ran -> touch no state.
        self.assertEqual(refresh_state_action(None), "none")

    def test_local_step_failure_is_not_an_outage(self):
        # The bug guard: step0 succeeded (fetch_ok=True) but a downstream LOCAL
        # step (step3 scoring) failed. _run_pipeline returns (rc=2, fetch_ok=True);
        # attribution must be record_success (reset throttle), NOT record_failure
        # (which would wrongly enter portal outage backoff for 30 min).
        self.assertEqual(refresh_state_action(True), "record_success")


class SearchCacheReconciliation(unittest.TestCase):
    """A warm-cache re-run must still reconcile. The cache-skip branch counts
    each cached page's records toward records_seen; otherwise `reconciled`
    (records_seen == sum_window_counts) is permanently False on any re-run over
    cached pages, raising a false 'records_seen != sum_window_counts' anomaly
    warning even though every promised record is already on disk."""

    def _audit(self):
        return {"windows": [], "sum_window_counts": 0, "pages_fetched": 0,
                "pages_skipped": 0, "records_seen": 0, "errors": []}

    def test_cached_pages_count_toward_records_seen(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            raw_dir = Path(td)
            wd = raw_dir / "2026"          # full-year window -> _label() == "2026"
            wd.mkdir()
            # count=3, page_size=2 -> 2 pages (2 + 1 records), both pre-cached.
            (wd / "page_001.json").write_text(
                json.dumps({"EntityResults": [{"CaseId": "a"}, {"CaseId": "b"}]}))
            (wd / "page_002.json").write_text(
                json.dumps({"EntityResults": [{"CaseId": "c"}]}))
            sess = _FakePostSession(total=3)
            audit = self._audit()
            fetchmod._fetch_window(sess, 2, "ApplyDate",
                                   dt.date(2026, 1, 1), dt.date(2026, 12, 31),
                                   raw_dir, 2, 0.0, False, audit, lambda *_: None)
            self.assertEqual(audit["pages_skipped"], 2)
            self.assertEqual(audit["pages_fetched"], 0)   # nothing re-fetched
            self.assertEqual(audit["sum_window_counts"], 3)
            self.assertEqual(audit["records_seen"], 3)    # cached pages counted
            # the reconciliation the entrypoint computes is now True on warm cache
            self.assertEqual(audit["records_seen"], audit["sum_window_counts"])

    def test_single_day_over_cap_clips_pages_to_window_cap(self):
        # An un-splittable single-day window whose count EXCEEDS the 10k offset
        # cap must clip its page loop at RESULT_WINDOW_CAP // page_size, not try
        # to page past from+size > cap (which the portal rejects, erroring out
        # the rest of the year). count must be > cap (not ==): count == cap pages
        # cleanly to exactly cap//page_size pages.
        import tempfile
        cap = fetchmod.RESULT_WINDOW_CAP
        with tempfile.TemporaryDirectory() as td:
            raw_dir = Path(td)
            day = dt.date(2026, 3, 4)
            sess = _FakePostSession(total=cap + 250)   # > cap, single day
            audit = self._audit()
            self._sleep = fetchmod.time.sleep
            fetchmod.time.sleep = lambda *_: None
            try:
                fetchmod._fetch_window(sess, 2, "ApplyDate", day, day,
                                       raw_dir, 100, 0.0, True, audit,
                                       lambda *_: None)
            finally:
                fetchmod.time.sleep = self._sleep
            # Clipped to the last fetchable page; NOT ceil((cap+250)/100) = cap//100 + 3.
            self.assertEqual(audit["pages_fetched"], cap // 100)
            self.assertEqual(audit["windows"][0]["pages"], cap // 100)
            # The full count is still recorded, so reconciliation honestly flags
            # the unreachable surplus (records_seen < sum_window_counts) rather
            # than masking it.
            self.assertEqual(audit["sum_window_counts"], cap + 250)

    def test_unreadable_cached_page_left_uncounted(self):
        # A corrupt cache file is tolerated (no crash) but stays uncounted, so
        # reconciliation correctly flags the gap rather than masking it.
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            raw_dir = Path(td)
            wd = raw_dir / "2026"
            wd.mkdir()
            (wd / "page_001.json").write_text(
                json.dumps({"EntityResults": [{"CaseId": "a"}, {"CaseId": "b"}]}))
            (wd / "page_002.json").write_text("{not valid json")
            sess = _FakePostSession(total=3)
            audit = self._audit()
            fetchmod._fetch_window(sess, 2, "ApplyDate",
                                   dt.date(2026, 1, 1), dt.date(2026, 12, 31),
                                   raw_dir, 2, 0.0, False, audit, lambda *_: None)
            self.assertEqual(audit["records_seen"], 2)         # only the good page
            self.assertNotEqual(audit["records_seen"], audit["sum_window_counts"])


class _ScriptedPostSession:
    """Returns a scripted sequence of responses, one per .post() call."""
    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = 0

    def post(self, url, json=None, timeout=None):
        self.calls += 1
        return self._responses.pop(0)


class SearchRequestRetry(unittest.TestCase):
    """_request must ride through a 200 with a non-JSON body (the same blip
    class as detail.py): retry rather than let a ValueError escape and abort
    step0 (critical=True -> whole tick aborts + a spurious outage backoff)."""

    def setUp(self):
        self._sleep = fetchmod.time.sleep
        fetchmod.time.sleep = lambda *_: None

    def tearDown(self):
        fetchmod.time.sleep = self._sleep

    def test_200_unparseable_body_is_retried(self):
        sess = _ScriptedPostSession([
            _BadJsonResp(),
            _FakeResp(200, {"Success": True, "Result": {"TotalFound": 7}}),
        ])
        result = fetchmod._request(sess, {}, "count")
        self.assertEqual(result, {"TotalFound": 7})
        self.assertEqual(sess.calls, 2)

    def test_persistent_unparseable_200_raises_searcherror(self):
        sess = _ScriptedPostSession(
            [_BadJsonResp() for _ in range(fetchmod.MAX_RETRIES + 1)])
        with self.assertRaises(fetchmod.SearchError):
            fetchmod._request(sess, {}, "count")
        self.assertEqual(sess.calls, fetchmod.MAX_RETRIES + 1)


class LoadRunLedger(unittest.TestCase):
    """Every step reads its audit ledger then appends a run. A corrupt or
    wrong-shape ledger must not abort the step (esp. the critical step3, after its
    DB work already committed) -- start fresh instead."""

    def _f(self, d, text=None):
        p = Path(d) / "runs_permit.json"
        if text is not None:
            p.write_text(text, encoding="utf-8")
        return p

    def test_missing_returns_fresh(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(load_run_ledger(self._f(d), log=lambda *_: None),
                             {"schema_version": 1, "runs": []})

    def test_valid_ledger_round_trips(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            good = {"schema_version": 1, "runs": [{"run_id": "x"}]}
            self.assertEqual(
                load_run_ledger(self._f(d, json.dumps(good)), log=lambda *_: None),
                good)

    def test_corrupt_json_starts_fresh_not_raise(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            led = load_run_ledger(self._f(d, "{ truncated …"), log=lambda *_: None)
            self.assertEqual(led, {"schema_version": 1, "runs": []})
            led["runs"].append({"run_id": "y"})  # the .append must not crash

    def test_wrong_shape_starts_fresh(self):
        # Valid JSON but not a {runs: [...]} dict -> a bare list would crash the
        # step's runs["runs"].append; start fresh instead.
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(
                load_run_ledger(self._f(d, "[1, 2, 3]"), log=lambda *_: None),
                {"schema_version": 1, "runs": []})
            self.assertEqual(
                load_run_ledger(self._f(d, '{"runs": "notalist"}'),
                                log=lambda *_: None),
                {"schema_version": 1, "runs": []})

    def test_ledger_trimmed_to_cap_on_load(self):
        # A long-lived 10-min tick would grow the ledger forever; load_run_ledger
        # bounds it to the most recent LEDGER_MAX_RUNS so the file self-trims on
        # the next append+write. The KEPT runs must be the newest ones, in order.
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            over = LEDGER_MAX_RUNS + 137
            big = {"schema_version": 1,
                   "runs": [{"run_id": i} for i in range(over)]}
            led = load_run_ledger(self._f(d, json.dumps(big)), log=lambda *_: None)
            self.assertEqual(len(led["runs"]), LEDGER_MAX_RUNS)
            # newest kept (the last record survives), oldest dropped
            self.assertEqual(led["runs"][-1]["run_id"], over - 1)
            self.assertEqual(led["runs"][0]["run_id"], over - LEDGER_MAX_RUNS)

    def test_ledger_at_or_under_cap_is_untouched(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            exact = {"schema_version": 1,
                     "runs": [{"run_id": i} for i in range(LEDGER_MAX_RUNS)]}
            self.assertEqual(
                load_run_ledger(self._f(d, json.dumps(exact)), log=lambda *_: None),
                exact)


class ReengagementSegment(unittest.TestCase):
    """The --reengagement export: recently-expired residential target permits
    with a reachable contact, biggest first. Separate from the active funnel."""

    def _db(self):
        c = sqlite3.connect(":memory:")
        self.addCleanup(c.close)
        c.executescript("""
            CREATE TABLE sca_permits (case_id TEXT PRIMARY KEY, case_number TEXT,
                case_status TEXT, address_display TEXT, apply_date TEXT,
                description TEXT);
            CREATE TABLE sca_leads (case_id TEXT PRIMARY KEY, category TEXT,
                owner_name TEXT, property_owner_name TEXT, contact_role TEXT,
                owner_email TEXT, owner_phone TEXT);
            CREATE TABLE sca_permit_detail (case_id TEXT PRIMARY KEY, valuation REAL);
        """)
        # (case_id, status, category, valuation, reachable?) — only the first two
        # qualify: Expired + target + reachable + >= $50k.
        recs = [
            ("keep-big", "Expired", "ADDITION", 500000, "p@x.com", None),   # qualifies
            ("keep-sm", "Expired", "ADU", 80000, None, "555-1234567"),      # qualifies
            ("drop-active", "Issued", "NEW_SFR", 900000, "a@x.com", None),  # not Expired
            ("drop-cat", "Expired", "SUBTRADE", 600000, "b@x.com", None),   # wrong category
            ("drop-unreach", "Expired", "NEW_SFR", 700000, None, None),     # unreachable
            ("drop-cheap", "Expired", "REMODEL", 1000, "c@x.com", None),    # under $50k
            ("drop-old", "Expired", "ADDITION", 300000, "d@x.com", None),   # pre-2023
        ]
        for cid, st, cat, val, em, ph in recs:
            ad = "2019-01-01T00:00:00" if cid == "drop-old" else "2024-06-01T00:00:00"
            c.execute("INSERT INTO sca_permits VALUES (?,?,?,?,?,?)",
                      (cid, cid.upper(), st, f"{cid} ST", ad, "scope text"))
            c.execute("INSERT INTO sca_leads VALUES (?,?,?,?,?,?,?)",
                      (cid, cat, "Owner Name", "Real Owner", "OWNER", em, ph))
            c.execute("INSERT INTO sca_permit_detail VALUES (?,?)", (cid, val))
        c.commit()
        return c

    def test_only_qualifying_rows_biggest_first(self):
        rows = _reengagement_rows(self._db())
        cases = [r[0] for r in rows]   # case_number == case_id.upper()
        self.assertEqual(cases, ["KEEP-BIG", "KEEP-SM"])  # ordered by valuation DESC
        # tuple shape matches the CSV column count
        self.assertEqual(len(rows[0]), len(REENGAGEMENT_COLUMNS))

    def test_prefers_real_property_owner(self):
        rows = _reengagement_rows(self._db())
        owner_idx = REENGAGEMENT_COLUMNS.index("owner")
        self.assertEqual(rows[0][owner_idx], "Real Owner")  # property_owner_name wins


class ApplyMigrations(unittest.TestCase):
    """apply_migrations must be atomic per file: a migration that fails partway
    leaves the DB unchanged and unrecorded, so the next run can cleanly retry it
    (the ALTER TABLE ADD COLUMN family is not idempotent, so a half-applied +
    unrecorded migration would otherwise brick the pipeline on re-run)."""

    import tempfile

    def _write(self, d, name, sql):
        (Path(d) / name).write_text(sql, encoding="utf-8")

    def test_happy_path_applies_all_and_is_idempotent(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            self._write(d, "0001_a.sql", "CREATE TABLE t (id INTEGER);")
            self._write(d, "0002_b.sql", "ALTER TABLE t ADD COLUMN name TEXT;")
            conn = sqlite3.connect(":memory:")
            self.addCleanup(conn.close)
            self.assertEqual(apply_migrations(conn, Path(d)),
                             ["0001_a.sql", "0002_b.sql"])
            # Re-run: nothing re-applied (the non-idempotent ADD COLUMN is skipped).
            self.assertEqual(apply_migrations(conn, Path(d)), [])
            cols = {r[1] for r in conn.execute("PRAGMA table_info(t)")}
            self.assertEqual(cols, {"id", "name"})

    def test_failed_migration_rolls_back_and_is_not_recorded(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            self._write(d, "0001_a.sql", "CREATE TABLE t (id INTEGER);")
            # Second file: a valid ADD COLUMN followed by a broken statement, so
            # it fails *after* a DDL side effect — the exact partial-failure case.
            self._write(d, "0002_b.sql",
                        "ALTER TABLE t ADD COLUMN name TEXT;\n"
                        "ALTER TABLE t ADD COLUMN name TEXT;")  # duplicate -> error
            conn = sqlite3.connect(":memory:")
            self.addCleanup(conn.close)
            with self.assertRaises(sqlite3.OperationalError):
                apply_migrations(conn, Path(d))
            # The first file committed; the failed one did NOT record itself...
            recorded = {r[0] for r in conn.execute(
                "SELECT filename FROM _schema_migrations")}
            self.assertEqual(recorded, {"0001_a.sql"})
            # ...and its partial DDL was rolled back: 'name' must NOT exist, so a
            # corrected re-run can apply it cleanly.
            cols = {r[1] for r in conn.execute("PRAGMA table_info(t)")}
            self.assertEqual(cols, {"id"})

    def test_retry_after_fix_succeeds(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            self._write(d, "0001_a.sql", "CREATE TABLE t (id INTEGER);")
            self._write(d, "0002_b.sql", "ALTER TABLE t ADD COLUMN name TEXT;\nBOGUS;")
            conn = sqlite3.connect(":memory:")
            self.addCleanup(conn.close)
            with self.assertRaises(sqlite3.OperationalError):
                apply_migrations(conn, Path(d))
            # Operator fixes the migration; the rollback left no 'name' column,
            # so the corrected file applies without a duplicate-column crash.
            self._write(d, "0002_b.sql", "ALTER TABLE t ADD COLUMN name TEXT;")
            self.assertEqual(apply_migrations(conn, Path(d)), ["0002_b.sql"])
            cols = {r[1] for r in conn.execute("PRAGMA table_info(t)")}
            self.assertEqual(cols, {"id", "name"})


class MigrationSetStatus(unittest.TestCase):
    """The healthcheck migration invariant compares the SET of applied filenames
    to the SET on disk -- a count-only check (applied == on_disk) passes when the
    sets drift but stay equal-sized, leaving the schema silently wrong."""

    def test_in_sync_passes(self):
        files = {"0001_a.sql", "0002_b.sql"}
        ok, detail = migration_set_status(files, files)
        self.assertTrue(ok)
        self.assertEqual(detail, "2 applied / 2 on disk")

    def test_unapplied_file_fails_and_is_named(self):
        ok, detail = migration_set_status({"0001_a.sql"},
                                          {"0001_a.sql", "0002_b.sql"})
        self.assertFalse(ok)
        self.assertIn("UNAPPLIED: 0002_b.sql", detail)

    def test_orphaned_applied_file_fails_and_is_named(self):
        # A migration recorded as applied but whose file is gone from disk.
        ok, detail = migration_set_status({"0001_a.sql", "0002_b.sql"},
                                          {"0001_a.sql"})
        self.assertFalse(ok)
        self.assertIn("APPLIED-BUT-MISSING: 0002_b.sql", detail)

    def test_equal_counts_but_disjoint_sets_still_fails(self):
        # The exact false-confidence case a count check (1 == 1) would miss:
        # one file renamed/replaced -> one unapplied + one orphaned, counts match.
        ok, detail = migration_set_status({"0002_old.sql"}, {"0002_new.sql"})
        self.assertFalse(ok)
        self.assertIn("UNAPPLIED: 0002_new.sql", detail)
        self.assertIn("APPLIED-BUT-MISSING: 0002_old.sql", detail)


if __name__ == "__main__":
    unittest.main()

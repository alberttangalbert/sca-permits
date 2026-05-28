"""Regression tests for the pure pipeline logic (no DB, no network).

The scoring/clustering behavior is driven by editable rule tables, so these lock
in the decisions that matter — especially the hard-won bug guards:
  * #16  an "Agent for Owner" must classify AGENT, never OWNER
  * the trailing-space CustomField labels ('Number of Stories ')
  * SQL-literal escaping of quotes in the D1 export

Run:  python3 -m unittest discover -s tests   (or: python3 -m unittest tests.test_logic)
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from utils.normalize import split_address
from utils.step_1.parsing import map_entity, parse_page
from utils.step_2.parsing import (_custom_fields, _fnum, _holds_summary,
                                   normalize_role, parse_detail)
from utils.step_3.clustering import aggregate, cluster_key
from utils.step_3.scoring import (band, classify_type, pick_contacts,
                                   score_record, size_factor, status_factor)
import sqlite3

from step2_fetch_details import select_case_ids
from step2_parse_details import unparsed_files
from step4_sync_d1 import _lit, _prune_statement


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


class Roles(unittest.TestCase):
    def test_agent_for_owner_is_agent_not_owner(self):  # bug #16
        self.assertEqual(normalize_role("Agent for Owner"), "AGENT")
        self.assertEqual(normalize_role("Owner's Agent"), "AGENT")

    def test_plain_roles(self):
        self.assertEqual(normalize_role("Owner"), "OWNER")
        self.assertEqual(normalize_role("General Contractor"), "CONTRACTOR")
        self.assertEqual(normalize_role("Applicant"), "APPLICANT")
        self.assertIsNone(normalize_role(None))


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

    def test_band_boundaries(self):
        self.assertEqual(band(50), "HIGH")
        self.assertEqual(band(49.9), "MEDIUM")
        self.assertEqual(band(22), "MEDIUM")
        self.assertEqual(band(21.9), "LOW")
        self.assertEqual(band(7), "LOW")
        self.assertEqual(band(6.9), "DROP")


class Contacts(unittest.TestCase):
    def test_prefers_owner_with_contact_and_flags_contractor(self):
        contacts = [
            {"role": "OWNER", "full_name": "Jane Doe", "company": None,
             "email": "jane@x.com", "phone": "555-1"},
            {"role": "APPLICANT", "full_name": "Appy", "company": None,
             "email": None, "phone": None},
            {"role": "CONTRACTOR", "full_name": "Bob", "company": "BuildCo",
             "email": None, "phone": "555-2"},
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
             "phone": "555-9"}])
        self.assertEqual(picked["owner_phone"], "555-9")
        self.assertEqual(picked["owner_name"], "Reachable App")

    def test_reachable_owner_still_wins_over_applicant(self):
        # When the owner IS reachable, keep preferring the owner.
        picked = pick_contacts([
            {"role": "OWNER", "full_name": "Owner", "email": "o@x", "phone": None},
            {"role": "APPLICANT", "full_name": "App", "email": "a@x",
             "phone": "555-1"}])
        self.assertEqual(picked["owner_name"], "Owner")
        self.assertEqual(picked["owner_email"], "o@x")


class ScoreRecord(unittest.TestCase):
    def test_blocking_hold_and_contractor_factors(self):
        base = dict(case_type="Building Residential-New Single Family",
                    case_status="Approved", description=None, valuation=600000,
                    contacts=[{"role": "OWNER", "full_name": "O", "email": "o@x",
                               "phone": "1"}])
        clean = score_record(**base)
        self.assertEqual(clean["lead_score"], 100.0)        # 1*1*1*1
        self.assertEqual(clean["lead_band"], "HIGH")
        # A contractor present and a blocking hold each shave the score.
        withc = score_record(**{**base, "contacts": [
            {"role": "CONTRACTOR", "company": "C"}]}, blocking_hold_count=1)
        self.assertEqual(withc["blocking_hold"], 1)
        self.assertAlmostEqual(withc["lead_score"], 100 * 0.8 * 0.9, places=4)


class Clustering(unittest.TestCase):
    def test_key_precedence(self):
        self.assertEqual(cluster_key("050011210", "123 main", "CID"),
                         ("P:050011210", "PARCEL"))
        self.assertEqual(cluster_key(None, "123 main", "CID"),
                         ("A:123 main", "ADDRESS"))
        self.assertEqual(cluster_key("  ", "", "CID"), ("C:CID", "SINGLETON"))

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


class SqlLiteral(unittest.TestCase):
    def test_lit_escaping(self):
        self.assertEqual(_lit(None), "NULL")
        self.assertEqual(_lit(True), "1")
        self.assertEqual(_lit(False), "0")
        self.assertEqual(_lit(5), "5")
        self.assertEqual(_lit(5.5), "5.5")
        self.assertEqual(_lit("plain"), "'plain'")
        self.assertEqual(_lit("it's a 10'-0\" deck"), "'it''s a 10''-0\" deck'")


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


class MissingDetailSelection(unittest.TestCase):
    """The historical-backfill selector: --missing-detail picks only permits with
    no parsed detail row, newest-first, respecting the chunk limit."""

    def _db(self):
        c = sqlite3.connect(":memory:")
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


if __name__ == "__main__":
    unittest.main()

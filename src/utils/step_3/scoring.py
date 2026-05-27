"""Step 3 scoring — turn an enriched permit into a banded lead.

Multiplicative gates×factors model (the cu-permits architecture, re-derived for
San Carlos's real vocabulary):

    lead_score = 100 · type_fit · size_factor · status_factor · contractor_factor

type_fit comes from `type_fit_rules.json` (ordered substring match on case_type,
with a description-keyword upgrade for the otherwise-uninformative misc/unknown
bucket). The other factors are defined here so a tuning change is a one-file
edit + a re-run (no re-fetch).

Carries the cu-permits audit fixes: #16 (an "agent" role never counts as a
contractor/owner), #17 (size from a real valuation signal, not fees), #19
(status/workflow liveness, not a hard day cutoff — done/expired score ≈ 0 by
bucket), #20 (pending-payment statuses are near-issuance).
"""

from __future__ import annotations

import json
from pathlib import Path

_RULES_PATH = Path(__file__).with_name("type_fit_rules.json")
_RULES = json.loads(_RULES_PATH.read_text())
TYPE_RULES = _RULES["rules"]
TYPE_FALLBACK = _RULES["fallback"]

# --- status → bucket → factor -------------------------------------------------
# Exact (lowercased) status string → bucket. Near-issuance (the GC sweet spot:
# city approval secured, work not yet underway) scores highest. "Fees Due" /
# "Fees Paid" are pending-payment near-issuance (bug #20). Done/dead ≈ 0.
STATUS_BUCKETS = {
    "approved": "READY_TO_ISSUE",
    "fees due": "READY_TO_ISSUE",
    "fees paid": "READY_TO_ISSUE",
    "in review": "IN_REVIEW",
    "submitted": "IN_REVIEW",
    "submitted - online": "IN_REVIEW",
    "issued": "ISSUED",
    "on hold": "ON_HOLD",
    "stop work order": "ON_HOLD",
    "finaled": "COMPLETE",
    "closed": "COMPLETE",
    "expired": "DEAD",
    "plan approval expired": "DEAD",
    "cancelled": "DEAD",
    "void": "DEAD",
    "denied": "DEAD",
}

STATUS_FACTOR = {
    "READY_TO_ISSUE": 1.0,
    "IN_REVIEW": 0.7,
    "ISSUED": 0.45,
    "ON_HOLD": 0.3,
    "COMPLETE": 0.05,
    "DEAD": 0.03,
    "UNKNOWN": 0.4,
}

# Keywords that upgrade a misc/unknown row (only when the type rule yielded the
# OTHER category — never used to downgrade, and never overrides an explicit type).
_DESC_UPGRADES = [
    (("accessory dwelling", "second dwelling", "second unit", "granny", " adu", "(adu", "adu "), 1.0, "ADU"),
    (("new single family", "new sfr", "new dwelling", "new home", "new residence"), 1.0, "NEW_SFR"),
    (("addition",), 0.9, "ADDITION"),
    (("remodel", "renovation"), 0.6, "REMODEL"),
]

BANDS = (("HIGH", 50.0), ("MEDIUM", 22.0), ("LOW", 7.0))  # else DROP


def classify_type(case_type: str | None, description: str | None) -> tuple[float, str]:
    """(type_fit, category) from the ordered rules; misc/unknown rows get a
    description-keyword upgrade so a real ADU/addition mistyped as 'Miscellaneous'
    still surfaces."""
    t = (case_type or "").lower()
    type_fit, category = TYPE_FALLBACK["type_fit"], TYPE_FALLBACK["category"]
    for rule in TYPE_RULES:
        if rule["match"] in t:
            type_fit, category = rule["type_fit"], rule["category"]
            break
    if category == "OTHER":
        d = (description or "").lower()
        for keywords, fit, cat in _DESC_UPGRADES:
            if any(k in d for k in keywords):
                return max(type_fit, fit), cat
    return type_fit, category


def status_bucket(case_status: str | None) -> str:
    return STATUS_BUCKETS.get((case_status or "").strip().lower(), "UNKNOWN")


def status_factor(case_status: str | None) -> tuple[float, str]:
    bucket = status_bucket(case_status)
    return STATUS_FACTOR[bucket], bucket


def size_factor(valuation: float | None) -> float:
    """Bucketed project size. Missing/0 valuation → neutral 0.35 (don't zero a
    real project just because the field is blank)."""
    if not valuation or valuation <= 0:
        return 0.35
    if valuation < 5_000:
        return 0.2
    if valuation < 20_000:
        return 0.35
    if valuation < 50_000:
        return 0.5
    if valuation < 100_000:
        return 0.65
    if valuation < 250_000:
        return 0.8
    if valuation < 500_000:
        return 0.9
    return 1.0


def band(score: float) -> str:
    for name, threshold in BANDS:
        if score >= threshold:
            return name
    return "DROP"


def pick_contacts(contacts: list[dict]) -> dict:
    """Choose the best owner/applicant and contractor contacts for outreach.

    Owner preference: a role==OWNER with an email > any OWNER > an APPLICANT
    (for residential the applicant is usually the owner). Contractor: the first
    role==CONTRACTOR; its presence is the `has_contractor` signal (bug #16: the
    `role` field already keeps 'Agent for Owner' out of OWNER/CONTRACTOR)."""
    owners = [c for c in contacts if c.get("role") == "OWNER"]
    applicants = [c for c in contacts if c.get("role") == "APPLICANT"]
    contractors = [c for c in contacts if c.get("role") == "CONTRACTOR"]

    def best(cands):
        if not cands:
            return None
        return max(cands, key=lambda c: (bool(c.get("email")), bool(c.get("phone")),
                                         bool(c.get("full_name"))))

    owner = best(owners) or best(applicants)
    contractor = best(contractors)
    return {
        "owner_name": (owner or {}).get("full_name") or (owner or {}).get("company"),
        "owner_email": (owner or {}).get("email"),
        "owner_phone": (owner or {}).get("phone"),
        "contractor_name": (contractor or {}).get("company")
                            or (contractor or {}).get("full_name") if contractor else None,
        "has_contractor": 1 if contractors else 0,
    }


def score_record(case_type, case_status, description, valuation, contacts) -> dict:
    """Compute the full lead row (factors + band + best contacts) for one permit."""
    type_fit, category = classify_type(case_type, description)
    sf = size_factor(valuation)
    stf, bucket = status_factor(case_status)
    picked = pick_contacts(contacts)
    contractor_factor = 0.8 if picked["has_contractor"] else 1.0

    score = round(100 * type_fit * sf * stf * contractor_factor, 1)
    return {
        "lead_score": score,
        "lead_band": band(score),
        "category": category,
        "type_fit": type_fit,
        "size_factor": sf,
        "status_factor": stf,
        "contractor_factor": contractor_factor,
        "status_bucket": bucket,
        "valuation": valuation,
        **picked,
    }

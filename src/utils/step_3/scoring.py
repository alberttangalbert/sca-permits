"""Step 3 scoring — turn an enriched permit into a banded lead.

Multiplicative gates×factors model (the cu-permits architecture, re-derived for
San Carlos's real vocabulary):

    lead_score = 100 · type_fit · size_factor · status_factor · contractor_factor
                     · hold_factor · recency_factor

type_fit comes from `type_fit_rules.json` (ordered substring match on case_type,
with a description-keyword upgrade for the otherwise-uninformative misc/unknown
bucket). The other factors are defined here so a tuning change is a one-file
edit + a re-run (no re-fetch).

Carries the cu-permits audit fixes: #16 (an "agent" role never counts as a
contractor/owner), #17 (size from a real valuation signal, not fees), #19
(status/workflow liveness, not a hard day cutoff — done/expired score ≈ 0 by
bucket), #20 (pending-payment statuses are near-issuance).

recency_factor (added 2026-05-29) decays the score with permit age. #19 trusted
STATUS alone for liveness, which is right for a live-from-day-one system — but
San Carlos CSS holds MIGRATED history where pre-cutover permits froze at an
actionable status ("Approved") and never advanced to "Finaled". Without recency,
a 2004 approved SFR scored like a 2026 one (28% of the actionable funnel was
>5y-old dead records). It's a soft multiplier (not a hard cutoff, in #19's
spirit): old permits fade out of HIGH/MEDIUM rather than being deleted.
"""

from __future__ import annotations

import datetime as dt
import json
import re
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


# City workers sometimes void a permit by editing the DESCRIPTION ("VOID WRONG
# PERMIT TYPE ...", "DUPLICATE PERMIT ...") without flipping case_status to
# Void. 2 such permits (BLDR2026-00220, BLDR2026-00057) currently sit at
# 'Submitted - Online' yet self-describe as voided -- they were leaking
# through as MEDIUM-band actionable leads on 2026-05-29. Detecting the
# description prefix lets us treat them as DEAD without waiting on the city
# to fix the status field.
_DESCRIPTION_VOID = re.compile(
    r"^\s*(void|wrong permit type|duplicate permit|repeat permit|voided)",
    re.IGNORECASE)


def looks_voided(description: str | None) -> bool:
    return bool(description) and bool(_DESCRIPTION_VOID.match(description))


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


# Age (years since apply_date) → decay multiplier. A lead's value as NEW work
# falls as the permit ages; this also demotes migrated records frozen at an
# actionable status. Buckets (user-chosen 2026-05-29) are upper-inclusive.
_RECENCY_BUCKETS = ((1.0, 1.0), (2.0, 0.8), (3.0, 0.55), (5.0, 0.3))
_RECENCY_FLOOR = 0.1  # older than the last bucket edge


def recency_factor(apply_date: str | None, today: dt.date | None = None) -> float:
    """Decay multiplier from permit age. A missing/unparseable apply_date returns
    a neutral 1.0 (never penalize a lead for a data gap); a future date (data
    error) is treated as brand-new. Pure for testability — pass `today`."""
    if not apply_date:
        return 1.0
    try:
        applied = dt.date.fromisoformat(apply_date[:10])
    except ValueError:
        return 1.0
    today = today or dt.date.today()
    years = (today - applied).days / 365.25
    for edge, factor in _RECENCY_BUCKETS:
        if years <= edge:
            return factor
    return _RECENCY_FLOOR


# EnerGov / Tyler placeholder identities. These appear in the source data with
# no real homeowner identity attached: 'void void' is the redacted-applicant
# placeholder (~65 rows, all unreachable); 'builder owner' is the owner-builder
# generic stamp (~5,000 rows, almost always with a stock 925-area phone that
# doesn't route to the homeowner). Surfacing them as `owner_name` on a lead is
# noise — the GC's call list ends up with "Call void void at [blank]" rows, or
# "BUILDER OWNER" rows where the phone is a placeholder. Filter at the
# candidate-pool level so the lead falls through to a real contact if one
# exists, or becomes properly unreachable (and the WARN healthcheck flags it).
_PLACEHOLDER_NAMES = {"void void", "builder owner", "test test", "redacted redacted"}


def _is_placeholder(c: dict) -> bool:
    name = (c.get("full_name") or "").strip().lower()
    return name in _PLACEHOLDER_NAMES


# Source-data hygiene helpers (added 2026-05-29). EnerGov contact rows
# sometimes put a phone in the email field, or an email with two @'s, or a
# 7-digit "phone" that's missing the area code. Treating those as reachable
# in the candidate ranking lets garbage beat a valid lower-tier contact AND
# pushes unusable data to D1. These validators are intentionally strict
# (require @ + a dot, require 10 digits) so only clearly-usable info counts.
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _email_usable(s: str | None) -> bool:
    return bool(s) and bool(_EMAIL_RE.match(s.strip()))


def _phone_usable(s: str | None) -> bool:
    if not s:
        return False
    digits = re.sub(r"\D", "", s)
    # 10 = US local + area code; 11 = with country prefix; up to 15 = E.164.
    return 10 <= len(digits) <= 15


def _usable_contact(c: dict) -> dict:
    """Return a view of the contact with unusable email/phone NULL'd out, so
    'IGOR@SLUTSKER@GMAIL.COM' or a 7-digit 'phone' can't beat a real entry."""
    return {
        **c,
        "email": c.get("email") if _email_usable(c.get("email")) else None,
        "phone": c.get("phone") if _phone_usable(c.get("phone")) else None,
    }


def pick_contacts(contacts: list[dict]) -> dict:
    """Choose the best outreach contact for a lead (and the contractor signal).

    Fallback chain (try each tier; the first whose best candidate is reachable
    wins; if none is reachable, fall back to the strongest named contact from
    the first tier that had one):

        OWNER -> APPLICANT -> ARCHITECT -> DESIGNER -> ENGINEER -> AGENT

    OWNER first because they're the buyer. APPLICANT second because on
    residential permits the applicant is usually the owner or their direct
    rep. ARCHITECT third because design-build leads route through the
    architect — calling MILLER JIM the architect about Sanjay's new SFR is
    a perfectly valid GC first move (audit 2026-05-29: ~10 HIGH/MEDIUM
    leads had a reachable architect when the owner row was contactless).
    DESIGNER fourth — same role for smaller residential jobs (ADU/REMODEL/
    ADDITION); the 2026-05-29 audit found 3 more unreachable MEDIUM leads
    had a fully-reachable Designer with email+phone. ENGINEER fifth (54%
    email, weaker B2B fit but still a real pro). AGENT last because the
    role has only 4% email/phone reachability — almost always procedural,
    last-resort only when no design or engineering pro is on file.

    The `contact_role` in the returned dict labels which tier won, so the
    GC's call list can show "MILLER JIM (Architect)" rather than implying
    the architect is the homeowner.

    Contractor: any role==CONTRACTOR; its presence is the `has_contractor`
    competitive signal. Placeholder identities ('void void', 'builder owner')
    are dropped from BOTH pools so they never surface as a lead contact and
    'BUILDER OWNER' (owner-builder stamp) never falsely triggers the
    contractor-attached penalty."""
    # Two cleanups before ranking: drop placeholder identities entirely
    # ('void void', 'BUILDER OWNER'), and NULL-out clearly-unusable email/
    # phone values so a typo email or a 5-digit "phone" can't beat a real
    # lower-tier contact in the reachability sort.
    real = [_usable_contact(c) for c in contacts if not _is_placeholder(c)]
    fallback_order = ("OWNER", "APPLICANT", "ARCHITECT", "DESIGNER",
                      "ENGINEER", "AGENT")
    by_role = {role: [c for c in real if c.get("role") == role]
               for role in fallback_order}
    contractors = [c for c in real if c.get("role") == "CONTRACTOR"]

    def best(cands):
        if not cands:
            return None
        return max(cands, key=lambda c: (bool(c.get("email")), bool(c.get("phone")),
                                         bool(c.get("full_name"))))

    def reachable(c):
        return bool(c and (c.get("email") or c.get("phone")))
    best_by_role = {r: best(by_role[r]) for r in fallback_order}

    contact = None
    contact_role = None
    for r in fallback_order:
        if reachable(best_by_role[r]):
            contact, contact_role = best_by_role[r], r
            break
    if contact is None:
        # No reachable candidate anywhere — fall back to the strongest named
        # contact from the first tier that had one, so the lead at least shows
        # a real name (the WARN healthcheck correctly counts it unreachable).
        for r in fallback_order:
            if best_by_role[r] is not None:
                contact, contact_role = best_by_role[r], r
                break

    contractor = best(contractors)
    return {
        "owner_name": (contact or {}).get("full_name") or (contact or {}).get("company"),
        "owner_email": (contact or {}).get("email"),
        "owner_phone": (contact or {}).get("phone"),
        "contact_role": contact_role,
        "contractor_name": ((contractor or {}).get("company")
                            or (contractor or {}).get("full_name")) if contractor else None,
        "has_contractor": 1 if contractors else 0,
    }


def score_record(case_type, case_status, description, valuation, contacts, *,
                 additional_sqft=None, num_stories=None, construction_type=None,
                 blocking_hold_count=0, apply_date=None, today=None) -> dict:
    """Compute the full lead row (factors + band + best contacts) for one permit.

    A blocking hold (active, non-expired) means the project is stuck at the city,
    so it's lightly de-prioritized (hold_factor 0.9) and flagged for the caller.
    apply_date drives the recency decay (stale/migrated permits fade out of the
    actionable funnel). additional_sqft / num_stories / construction_type are
    carried as lead context (they qualify the job on a sales call); they don't
    drive the score — every record that has them also has a valuation, so size
    is already covered."""
    type_fit, category = classify_type(case_type, description)
    sf = size_factor(valuation)
    stf, bucket = status_factor(case_status)
    # Description-based void detection: when a permit self-describes as
    # voided (city edited the description but forgot to flip status), force
    # the DEAD status bucket so it doesn't ride status="Submitted" into the
    # actionable funnel. This is the smallest possible override -- only
    # cuts in when status_bucket isn't already DEAD/COMPLETE.
    if bucket not in ("DEAD", "COMPLETE") and looks_voided(description):
        stf, bucket = STATUS_FACTOR["DEAD"], "DEAD"
    picked = pick_contacts(contacts)
    contractor_factor = 0.8 if picked["has_contractor"] else 1.0
    blocking = 1 if (blocking_hold_count or 0) > 0 else 0
    hold_factor = 0.9 if blocking else 1.0
    rf = recency_factor(apply_date, today)

    score = round(100 * type_fit * sf * stf * contractor_factor * hold_factor * rf, 1)
    return {
        "lead_score": score,
        "lead_band": band(score),
        "category": category,
        "type_fit": type_fit,
        "size_factor": sf,
        "status_factor": stf,
        "contractor_factor": contractor_factor,
        "recency_factor": rf,
        "status_bucket": bucket,
        "valuation": valuation,
        "additional_sqft": additional_sqft,
        "num_stories": num_stories,
        "construction_type": construction_type,
        "blocking_hold": blocking,
        **picked,
    }

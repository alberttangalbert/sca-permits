"""Step 3b clustering — fold a parcel's scored permits into one project lead.

cluster_key chooses the most reliable grouping key available (cu-permits bug
#11/#14: prefer a real key, never collapse an empty one):

    main_parcel APN  ->  address_norm  ->  case_id (a singleton)

aggregate() summarizes a project: its strength is the best permit's score, its
anchor is that permit, and the outreach contact is the most complete owner found
anywhere in the project (preferring the anchor's).
"""

from __future__ import annotations

import re
from collections import defaultdict


def canonical_parcel_by_address(address_parcel_pairs) -> dict[str, str]:
    """Map address_norm -> its canonical parcel, ONLY for addresses where every
    permit that names a parcel agrees on ONE distinct parcel.

    step3b uses this so a NULL-parcel permit can adopt the parcel its siblings
    at the same address already carry, letting them cluster as one project
    (5 actionable clusters were split this way pre-fix, 2026-05-29). The
    single-distinct guard is the hard-won part: an address with MULTIPLE
    distinct parcels is a multi-unit building that legitimately has a parcel
    per unit, so adopting one would over-collapse unrelated projects -- such
    addresses are deliberately omitted. Blank/None address or parcel is ignored.
    """
    seen: dict[str, set] = defaultdict(set)
    for addr, parcel in address_parcel_pairs:
        a = (addr or "").strip()
        p = (parcel or "").strip()
        if a and p:
            seen[a].add(p)
    return {a: next(iter(ps)) for a, ps in seen.items() if len(ps) == 1}


# Single-word street-suffix tokens that aren't real addresses: an EnerGov
# data-entry mishap leaves the type suffix alone after city-tail stripping
# ("DR San Carlos CA 94070" -> address_norm "DR"). Keying on these merges
# unrelated permits into one bogus cluster (e.g. A:DR pulling together two
# cancelled permits at different physical sites). Detected 2026-05-29 (impact:
# 4 clusters, all DROP/Cancelled, 0 actionable). The set is conservative -- only
# the lone-suffix case is caught; "HIGHLANDS PARK" or "CORNER ARROYO / EL
# CAMINO REAL" stay valid ADDRESS keys because they're multi-word landmarks
# that legitimately cluster together.
_LONE_STREET_SUFFIX = {"ST", "AVE", "BLVD", "DR", "RD", "LN", "CT", "WAY",
                       "TER", "PL", "CIR", "PKWY", "HWY", "PK"}


def cluster_key(main_parcel, address_norm, case_id, address_unit=None) -> tuple[str, str]:
    p = (main_parcel or "").strip()
    if p:
        return f"P:{p}", "PARCEL"
    a = (address_norm or "").strip()
    if a and a.upper() not in _LONE_STREET_SUFFIX:
        # Include the unit so a NULL-parcel permit in a multifamily building keys
        # PER UNIT, not by the bare street address. normalize.split_address()
        # deliberately holds the unit in a SEPARATE column precisely because
        # stripping it OVER-COLLAPSES multifamily buildings -- without this, e.g.
        # 14 distinct units at "1 LAUREL ST" (all parcel-less) merged into one
        # bogus "project" with mixed owners/contacts (independent review,
        # 2026-06-02). A unit-less address (the single-family norm) is unchanged.
        # Normalize the unit (lowercase, drop punctuation/space) so EnerGov's
        # inconsistent formatting -- "# 106" vs "106" for the SAME unit -- doesn't
        # over-split it into two clusters.
        u = re.sub(r"[^a-z0-9]", "", (address_unit or "").lower())
        return (f"A:{a}|{u}" if u else f"A:{a}"), "ADDRESS"
    return f"C:{case_id}", "SINGLETON"


def _contact_completeness(m: dict) -> tuple:
    return (bool(m.get("owner_email")), bool(m.get("owner_phone")),
            bool(m.get("owner_name")))


def aggregate(cluster_id: str, key_type: str, members: list[dict]) -> dict:
    """Build the one-row project summary for a cluster's member leads."""
    # Anchor: highest score, then biggest valuation, then newest application.
    anchor = max(members, key=lambda m: (
        m.get("lead_score") or 0.0, m.get("valuation") or 0.0,
        m.get("apply_date") or ""))
    # Outreach contact: PREFER the anchor when its owner is reachable -- the
    # anchor is the highest-scoring permit (typically the actual NEW_SFR /
    # ADDITION project) and its surfaced owner_* already represents the best
    # tier from pick_contacts on that permit. Falling through to a more-complete
    # sibling permit's contact would replace e.g. "Sharron William (Owner)" on a
    # new SFR with "Valley Heating (Applicant)" from a sub-trade HVAC permit at
    # the same parcel -- the GC would think they're calling the homeowner when
    # they're really calling the HVAC contractor. Only fall through when the
    # anchor truly has no email AND no phone.
    def _reachable(m):
        return bool(m.get("owner_email")) or bool(m.get("owner_phone"))

    if _reachable(anchor):
        contact = anchor
    else:
        contact = max(members, key=lambda m: (
            _contact_completeness(m), m is anchor))

    vals = [m.get("valuation") or 0.0 for m in members]
    cats = sorted({m.get("category") for m in members if m.get("category")})
    applies = [m.get("apply_date") for m in members if m.get("apply_date")]

    return {
        "cluster_id": cluster_id,
        "key_type": key_type,
        "permit_count": len(members),
        "max_lead_score": anchor.get("lead_score"),
        "top_band": anchor.get("lead_band"),
        "categories": ", ".join(cats) or None,
        "total_valuation": round(sum(vals), 2) if any(vals) else None,
        "max_valuation": max(vals) if any(vals) else None,
        "primary_case_id": anchor.get("case_id"),
        "address_display": anchor.get("address_display"),
        "main_parcel": anchor.get("main_parcel"),
        "owner_name": contact.get("owner_name"),
        "owner_email": contact.get("owner_email"),
        "owner_phone": contact.get("owner_phone"),
        # Echo the picked contact's role so the cluster export can show
        # "MILLER JIM (Architect)" the same way the per-permit list does.
        "contact_role": contact.get("contact_role"),
        # The real property owner's name for the project: prefer the anchor's,
        # but fall through to ANY member that has one -- an owner named on a
        # sibling permit (e.g. an older parcel permit) still identifies the
        # homeowner even when the anchor permit only named a designer.
        "property_owner_name": (anchor.get("property_owner_name")
                                or next((m.get("property_owner_name") for m in members
                                         if m.get("property_owner_name")), None)),
        "has_contractor": 1 if any(m.get("has_contractor") for m in members) else 0,
        "first_apply_date": min(applies) if applies else None,
        "last_apply_date": max(applies) if applies else None,
    }

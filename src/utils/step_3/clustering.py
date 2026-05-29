"""Step 3b clustering — fold a parcel's scored permits into one project lead.

cluster_key chooses the most reliable grouping key available (cu-permits bug
#11/#14: prefer a real key, never collapse an empty one):

    main_parcel APN  ->  address_norm  ->  case_id (a singleton)

aggregate() summarizes a project: its strength is the best permit's score, its
anchor is that permit, and the outreach contact is the most complete owner found
anywhere in the project (preferring the anchor's).
"""

from __future__ import annotations


def cluster_key(main_parcel, address_norm, case_id) -> tuple[str, str]:
    p = (main_parcel or "").strip()
    if p:
        return f"P:{p}", "PARCEL"
    a = (address_norm or "").strip()
    if a:
        return f"A:{a}", "ADDRESS"
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
        "has_contractor": 1 if any(m.get("has_contractor") for m in members) else 0,
        "first_apply_date": min(applies) if applies else None,
        "last_apply_date": max(applies) if applies else None,
    }

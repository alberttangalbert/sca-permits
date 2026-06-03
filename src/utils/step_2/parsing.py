"""Step 2 parsing — map a record's detail JSON to detail + contact rows.

Input is the `Result` envelope from `GET permits/permit/<CaseId>`. We produce:
  * one sca_permit_detail row (valuation, SF, parcel, embedded-list counts), and
  * zero-or-more sca_permit_contacts rows (owner/applicant/contractor/...).

Role handling carries cu-permits BUG #16: an "Agent for Owner" / "Owner's Agent"
must classify as AGENT, never OWNER — so the AGENT test runs before OWNER.
"""

from __future__ import annotations

DETAIL_COLUMNS = [
    "case_id", "valuation", "square_feet", "main_parcel", "parcel_count",
    "contact_count", "hold_count", "attachment_count",
    "permit_type_id", "permit_workclass_id", "is_renewal", "application_date",
    # added in migration 0004 — harvested from CustomFields[] / Holds[]
    "additional_sqft", "num_stories", "construction_type", "occupancy_class",
    "active_hold_count", "blocking_hold_count",
]

CONTACT_COLUMNS = [
    "case_id", "parent_contact_id", "global_entity_id", "contact_type_id",
    "role_raw", "role", "first_name", "last_name", "full_name", "company",
    "email", "phone", "phone_type", "contact_address", "is_billing",
]


def _s(v):
    """Trim a string field; ''/whitespace/non-str -> None."""
    if isinstance(v, str):
        v = v.strip()
        return v or None
    return None


def _as_list(v):
    """A source list field, or [] if it's anything else. `(x or [])` only guards
    None/empty -- a truthy NON-list (e.g. CustomFields returned as a string under
    EnerGov schema drift or a malformed 200) would iterate by character and then
    crash on `.get()`, poisoning the whole step2 parse (parse_detail isn't
    wrapped per-record). Coerce any non-list to [] so a shape change degrades to
    'no rows' instead of an exception."""
    return v if isinstance(v, list) else []


def _num(v):
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def _fnum(v):
    """Coerce a CustomField value (number or numeric string) to float, else None.
    Treats 0 / blank / 'None' as absent (these are EnerGov's unfilled defaults)."""
    if isinstance(v, bool) or v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v) or None
    if isinstance(v, str):
        try:
            return float(v.strip().replace(",", "")) or None
        except ValueError:
            return None
    return None


def _bint(v):
    """Coerce a JSON bool to 0/1 (None stays None)."""
    if v is None:
        return None
    return 1 if v else 0


def normalize_role(role_raw: str | None) -> str | None:
    """Map ContactTypeName to a coarse role bucket. AGENT is tested first so an
    'Agent for Owner' never collapses into OWNER (cu-permits bug #16).

    DESIGNER is a first-class role (added 2026-05-29 audit): "Designer" was the
    single biggest role_raw in the catch-all OTHER bucket (1,623 contacts,
    76%/89% email/phone reachability). For residential ADU/REMODEL/ADDITION
    leads where the owner is contactless, the designer IS a valid B2B
    contact — same as the architect, just for smaller jobs."""
    if not role_raw:
        return None
    r = role_raw.lower()
    if "agent" in r:
        return "AGENT"
    if "contractor" in r:
        return "CONTRACTOR"
    if "applicant" in r:
        return "APPLICANT"
    if "owner" in r:
        return "OWNER"
    if "architect" in r:
        return "ARCHITECT"
    if "designer" in r:
        return "DESIGNER"
    if "engineer" in r:
        return "ENGINEER"
    if "tenant" in r:
        return "TENANT"
    return "OTHER"


def _full_name(first: str | None, last: str | None) -> str | None:
    name = " ".join(p for p in (first, last) if p)
    return name or None


def _main_parcel(result: dict) -> str | None:
    """Best APN: the Main location address's ParcelNumber, else the first
    non-empty parcel number on any address/parcel."""
    addresses = _as_list(result.get("Addresses"))
    for a in addresses:
        if a.get("Main") and _s(a.get("ParcelNumber")):
            return _s(a.get("ParcelNumber"))
    for a in addresses:
        if _s(a.get("ParcelNumber")):
            return _s(a.get("ParcelNumber"))
    for p in (_as_list(result.get("Parcels"))):
        if _s(p.get("ParcelNumber")):
            return _s(p.get("ParcelNumber"))
    return None


def _custom_fields(result: dict) -> dict:
    """Map CustomFields to {stripped_lower_label: Value}. EnerGov's labels carry
    TRAILING SPACES ('Number of Stories ') — strip or every lookup silently misses
    (a bug caught in the step 0-3 audit)."""
    out = {}
    for cf in (_as_list(result.get("CustomFields"))):
        label = (cf.get("Label") or cf.get("FieldName") or "").strip().lower()
        if label and cf.get("Value") not in (None, ""):
            out[label] = cf.get("Value")
    return out


def _holds_summary(result: dict) -> tuple[int, int]:
    """(active_count, blocking_count). Blocking = active and not an 'Expired
    Permit Hold' (that type just mirrors Expired status, so it's not new signal)."""
    active = blocking = 0
    for h in (_as_list(result.get("Holds"))):
        if h.get("Active"):
            active += 1
            if "expired permit" not in (h.get("HoldTypeSetupName") or "").lower():
                blocking += 1
    return active, blocking


def parse_contacts(result: dict, case_id: str) -> list[dict]:
    rows = []
    for c in (_as_list(result.get("Contacts"))):
        first, last = _s(c.get("FirstName")), _s(c.get("LastName"))
        role_raw = _s(c.get("ContactTypeName"))
        rows.append({
            "case_id": case_id,
            "parent_contact_id": _s(c.get("ParentContactID")),
            "global_entity_id": _s(c.get("GlobalEntityID")),
            "contact_type_id": _s(c.get("ContactTypeID")),
            "role_raw": role_raw,
            "role": normalize_role(role_raw),
            "first_name": first,
            "last_name": last,
            "full_name": _full_name(first, last),
            "company": _s(c.get("GlobalEntityName")),
            "email": _s(c.get("EmailTo")),
            "phone": _s(c.get("Phone")),
            "phone_type": _s(c.get("PhoneType")),
            "contact_address": _s(c.get("MainAddress")),
            "is_billing": _bint(c.get("IsBilling")),
        })
    return rows


def parse_detail(result: dict, case_id: str) -> dict:
    cf = _custom_fields(result)
    active_holds, blocking_holds = _holds_summary(result)
    return {
        "case_id": case_id,
        "valuation": _num(result.get("ValuationValue")),
        "square_feet": _num(result.get("SquareFeet")),
        "main_parcel": _main_parcel(result),
        "parcel_count": len(_as_list(result.get("Parcels"))),
        "contact_count": len(_as_list(result.get("Contacts"))),
        "hold_count": len(_as_list(result.get("Holds"))),
        "attachment_count": len(_as_list(result.get("Attachments"))),
        "permit_type_id": _s(result.get("PermitTypeID")),
        "permit_workclass_id": _s(result.get("PermitWorkClassID")),
        "is_renewal": _bint(result.get("IsRenewal")),
        "application_date": _s(result.get("ApplicationDate")),
        "additional_sqft": _fnum(cf.get("additional square footage")),
        "num_stories": _fnum(cf.get("number of stories")),
        "construction_type": _s(cf.get("type of construction")) if isinstance(cf.get("type of construction"), str) else None,
        "occupancy_class": _s(cf.get("occupancy class")) if isinstance(cf.get("occupancy class"), str) else None,
        "active_hold_count": active_holds,
        "blocking_hold_count": blocking_holds,
    }

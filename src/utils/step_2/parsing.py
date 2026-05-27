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


def _num(v):
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def _bint(v):
    """Coerce a JSON bool to 0/1 (None stays None)."""
    if v is None:
        return None
    return 1 if v else 0


def normalize_role(role_raw: str | None) -> str | None:
    """Map ContactTypeName to a coarse role bucket. AGENT is tested first so an
    'Agent for Owner' never collapses into OWNER (cu-permits bug #16)."""
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
    addresses = result.get("Addresses") or []
    for a in addresses:
        if a.get("Main") and _s(a.get("ParcelNumber")):
            return _s(a.get("ParcelNumber"))
    for a in addresses:
        if _s(a.get("ParcelNumber")):
            return _s(a.get("ParcelNumber"))
    for p in (result.get("Parcels") or []):
        if _s(p.get("ParcelNumber")):
            return _s(p.get("ParcelNumber"))
    return None


def parse_contacts(result: dict, case_id: str) -> list[dict]:
    rows = []
    for c in (result.get("Contacts") or []):
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
    return {
        "case_id": case_id,
        "valuation": _num(result.get("ValuationValue")),
        "square_feet": _num(result.get("SquareFeet")),
        "main_parcel": _main_parcel(result),
        "parcel_count": len(result.get("Parcels") or []),
        "contact_count": len(result.get("Contacts") or []),
        "hold_count": len(result.get("Holds") or []),
        "attachment_count": len(result.get("Attachments") or []),
        "permit_type_id": _s(result.get("PermitTypeID")),
        "permit_workclass_id": _s(result.get("PermitWorkClassID")),
        "is_renewal": _bint(result.get("IsRenewal")),
        "application_date": _s(result.get("ApplicationDate")),
    }

"""Step 1 parsing — map EnerGov search JSON to sca_permits rows.

This is the JSON analog of cu-permits' BeautifulSoup grid parser, and it is far
smaller: the search response already hands back structured records. We read
Result.EntityResults[] and map each to a row dict keyed on the record GUID
(CaseId). No HTML, no span-ID suffixes.
"""

from __future__ import annotations

from utils.normalize import split_address

# sca_permits columns we populate from the search row (must match migration 0001).
COLUMNS = [
    "case_id", "case_number", "module", "module_id",
    "case_type", "case_type_id", "case_workclass", "case_workclass_id",
    "case_status", "case_status_id", "project_name",
    "apply_date", "issue_date", "expire_date", "final_date",
    "address_display", "address_norm", "address_unit", "main_parcel",
    "description", "source_page",
]


def _clean(v):
    """Normalize a scalar field: strip strings (""/whitespace -> None), pass
    through numbers/bools, and drop anything structured (EnerGov returns some
    fields, e.g. `Address`, as nested objects — we only persist scalars here)."""
    if v is None:
        return None
    if isinstance(v, str):
        v = v.strip()
        return v or None
    if isinstance(v, (int, float, bool)):
        return v
    return None


def map_entity(entity: dict, module_label: str, source_page: int) -> dict:
    """Map one Result.EntityResults[] object to an sca_permits row dict."""
    # AddressDisplay is the human string; `Address` is a structured object we skip.
    address_display = _clean(entity.get("AddressDisplay"))
    addr_norm, addr_unit = split_address(address_display)
    return {
        "case_id": _clean(entity.get("CaseId")),
        "case_number": _clean(entity.get("CaseNumber")),
        "module": module_label,
        "module_id": entity.get("ModuleName"),
        "case_type": _clean(entity.get("CaseType")),
        "case_type_id": _clean(entity.get("CaseTypeId")),
        "case_workclass": _clean(entity.get("CaseWorkclass")),
        "case_workclass_id": _clean(entity.get("CaseWorkclassId")),
        "case_status": _clean(entity.get("CaseStatus")),
        "case_status_id": _clean(entity.get("CaseStatusId")),
        "project_name": _clean(entity.get("ProjectName")),
        "apply_date": _clean(entity.get("ApplyDate")),
        "issue_date": _clean(entity.get("IssueDate")),
        "expire_date": _clean(entity.get("ExpireDate")),
        "final_date": _clean(entity.get("FinalDate")),
        "address_display": address_display,
        "address_norm": addr_norm or None,
        "address_unit": addr_unit or None,
        "main_parcel": _clean(entity.get("MainParcel")),
        "description": _clean(entity.get("Description")),
        "source_page": source_page,
    }


def parse_page(result: dict, module_label: str, source_page: int) -> list[dict]:
    """Map a cached page's Result envelope to a list of sca_permits row dicts,
    skipping any entity missing a CaseId (the primary key)."""
    rows = []
    # EntityResults must be a LIST. `(x or [])` only guards None/empty -- a truthy
    # non-list (schema drift / malformed 200) would iterate by character and crash
    # map_entity's .get(), aborting the critical parse step. Coerce to [] so a
    # shape change degrades to 'no rows' rather than poisoning the whole page.
    entities = result.get("EntityResults")
    for entity in (entities if isinstance(entities, list) else []):
        row = map_entity(entity, module_label, source_page)
        if row["case_id"]:
            rows.append(row)
    return rows

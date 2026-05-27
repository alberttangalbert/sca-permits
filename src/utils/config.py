"""Configuration + request builders for the San Carlos EnerGov CSS JSON API.

San Carlos runs Tyler EnerGov Citizen Self Service (CSS) — an AngularJS SPA over a
JSON REST API. Unlike the Accela cities (cu-permits) there is NO HTML/postback
layer: step 0 POSTs JSON to the search endpoint and caches the response; step 1
reads fields out of that JSON.

Verified against the live portal on 2026-05-26 (read-only recon, logged OUT):
  * POST .../api/energov/search/search  -> HTTP 200 with NO Authorization header
    => public search is ANONYMOUS; no OIDC token required.
  * A Permit search sends SearchModule=1 (All) + FilterModule=2 (Permit).
  * Paging is TOP-LEVEL PageNumber/PageSize; PageSize 100 is allowed (10/25/50/100).
  * Type/status filters use the sentinel string "none" (NOT null) when unfiltered.
  * Response: Result.EntityResults[]; Result.TotalFound; Result.TotalPages.
    Per record: CaseId (GUID — the step-2 detail key), CaseNumber, CaseType,
    CaseStatus, ProjectName, ApplyDate, AddressDisplay, MainParcel, Description,
    ModuleName.

The doctrine from cu-permits still holds: pull EVERYTHING (no type/status/date
filter — sentinels stay "none"/null) and filter/score downstream, so a scoring
change never forces a re-scrape.
"""

from __future__ import annotations

import copy

BASE_HOST = "https://sancarlosca-energovweb.tylerhost.net"
API_BASE = f"{BASE_HOST}/apps/selfservice/api/energov"
SEARCH_URL = f"{API_BASE}/search/search"

# Step 2 detail — "Route A": the raw record GET, keyed on the CaseId GUID as a
# PATH segment (verified 2026-05-27, anonymous, HTTP 200). Returns ValuationValue,
# SquareFeet, and embedded Contacts[]/Addresses[]/Parcels[]/Holds[] in one call.
# (The sibling `permits/permit?id=<GUID>` query-param route returns resolved
# type/status NAMES + dates, but we already have those from search — so unused.)
PERMIT_DETAIL_URL = f"{API_BASE}/permits/permit"  # + "/<CaseId>"


def permit_detail_url(case_id: str) -> str:
    return f"{PERMIT_DETAIL_URL}/{case_id}"

# SearchModule / FilterModule enum — read from the portal's app/energov JS bundle.
FILTER_MODULE = {
    "All": 1,
    "Permit": 2,
    "Plan": 3,
    "Inspection": 4,
    "CodeCase": 5,
    "Request": 6,
    "Business": 7,
    "BusinessLicense": 8,
}

# Modules this repo pulls. Plan deferred — Permit only for this build.
MODULES: dict[str, dict] = {
    "Permit": {
        "filter_module": FILTER_MODULE["Permit"],
        "raw_subdir": "permit",
        "sort_by": "PermitNumber.keyword",
        "purpose": "primary — construction, additions, alterations, operational",
    },
}

# Tyler tenant headers, captured from the live (logged-out) SPA request.
# The auth spike proved the MINIMAL required header set is just
# Accept + Content-Type + tenantId — dropping tenantId yields HTTP 500. The other
# three are sent for parity with the SPA but are not strictly required.
TENANT_ID = "1"                       # REQUIRED — without it the API 500s
TENANT_NAME = "cityofsancarlosca-prod"
TENANT_URL = "EnerGovProd"            # Tyler-TenantUrl
TENANT_CULTURE = "en-US"
CONTENT_TYPE = "application/json;charset=utf-8"

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

DEFAULT_PAGE_SIZE = 100  # allowed values: 10, 25, 50, 100


def _null_criteria(extra: dict | None = None) -> dict:
    """A criteria sub-object with everything null and paging zeroed — the shape
    every per-module criteria block takes when that module isn't being filtered.
    Top-level PageNumber/PageSize drive paging, so these stay 0."""
    base = {
        "ProjectName": None,
        "Address": None,
        "ParcelNumber": None,
        "Description": None,
        "SearchMainAddress": False,
        "ContactId": None,
        "ExcludeCases": None,
        "EnableDescriptionSearch": False,
        "PageNumber": 0,
        "PageSize": 0,
        "SortBy": None,
        "SortAscending": False,
    }
    if extra:
        base.update(extra)
    return base


# PermitCriteria — captured verbatim from the live SPA request (2026-05-26).
# Type/status use the "none" sentinel = "don't filter".
_PERMIT_CRITERIA = {
    "PermitNumber": None,
    "PermitTypeId": "none",
    "PermitWorkclassId": None,
    "PermitStatusId": "none",
    "ProjectName": None,
    "IssueDateFrom": None, "IssueDateTo": None,
    "Address": None,
    "Description": None,
    "ExpireDateFrom": None, "ExpireDateTo": None,
    "FinalDateFrom": None, "FinalDateTo": None,
    "ApplyDateFrom": None, "ApplyDateTo": None,
    "SearchMainAddress": False,
    "ContactId": None,
    "TypeId": None,
    "WorkClassIds": None,
    "ParcelNumber": None,
    "ExcludeCases": None,
    "EnableDescriptionSearch": False,
    "PageNumber": 0,
    "PageSize": 0,
    "SortBy": "PermitNumber.keyword",
    "SortAscending": False,
}

# PlanCriteria — captured verbatim (used when FilterModule=3; harmless otherwise).
_PLAN_CRITERIA = {
    "PlanNumber": None,
    "PlanTypeId": None,
    "PlanWorkclassId": None,
    "PlanStatusId": None,
    "ProjectName": None,
    "ApplyDateFrom": None, "ApplyDateTo": None,
    "ExpireDateFrom": None, "ExpireDateTo": None,
    "CompleteDateFrom": None, "CompleteDateTo": None,
    "Address": None,
    "Description": None,
    "SearchMainAddress": False,
    "ContactId": None,
    "ParcelNumber": None,
    "TypeId": None,
    "WorkClassIds": None,
    "ExcludeCases": None,
    "EnableDescriptionSearch": False,
    "PageNumber": 0,
    "PageSize": 0,
    "SortBy": None,
    "SortAscending": False,
}


def build_search_body(filter_module: int, page_number: int,
                      page_size: int = DEFAULT_PAGE_SIZE,
                      sort_by: str = "PermitNumber.keyword",
                      sort_ascending: bool = True,
                      search_module: int = 1,
                      apply_date_from: str | None = None,
                      apply_date_to: str | None = None) -> dict:
    """Build the search POST body for a given module + page.

    Two modes, both observed against the live API:

    * search_module=1 (All / global keyword search): the SPA's default. The
      server reads TOP-LEVEL PageNumber/PageSize and IGNORES the per-module
      criteria (so date/type filters do nothing). Offset paging is capped at
      10,000 results (Elasticsearch index.max_result_window).

    * search_module=2 (Permit-specific / "Advanced" search): the server reads
      paging AND filters from PermitCriteria. This is the mode that honors
      ApplyDateFrom/To — used to year-chunk the backfill under the 10k cap.

    All per-module criteria objects are sent (null/sentinel) for parity with the
    SPA; ASP.NET model binding tolerates the ones we don't populate.
    """
    permit = copy.deepcopy(_PERMIT_CRITERIA)
    permit["ApplyDateFrom"] = apply_date_from
    permit["ApplyDateTo"] = apply_date_to
    if search_module != FILTER_MODULE["All"]:
        # Module-specific search reads paging from the criteria object.
        permit["PageNumber"] = page_number
        permit["PageSize"] = page_size
        permit["SortBy"] = sort_by
        permit["SortAscending"] = sort_ascending
    return {
        "Keyword": "",
        "ExactMatch": True,
        "SearchModule": search_module,
        "FilterModule": filter_module,
        "SearchMainAddress": False,
        "PlanCriteria": copy.deepcopy(_PLAN_CRITERIA),
        "PermitCriteria": permit,
        "InspectionCriteria": _null_criteria({
            "InspectionNumber": None, "InspectionTypeId": None,
            "InspectionStatusId": None}),
        "CodeCaseCriteria": _null_criteria({
            "CaseTypeId": None, "CodeCaseStatusId": None, "RequestId": None}),
        "RequestCriteria": _null_criteria({
            "RequestNumber": None, "RequestTypeId": None,
            "RequestStatusId": None}),
        "BusinessLicenseCriteria": _null_criteria({
            "LicenseNumber": None, "LicenseTypeId": None,
            "LicenseStatusId": None}),
        "ProfessionalLicenseCriteria": _null_criteria({
            "LicenseNumber": None, "LicenseTypeId": None,
            "LicenseStatusId": None}),
        "LicenseCriteria": _null_criteria({
            "LicenseNumber": None, "LicenseTypeId": None,
            "LicenseStatusId": None}),
        "ProjectCriteria": _null_criteria({
            "ProjectNumber": None, "TypeId": None}),
        "ExcludeCases": None,
        "HiddenInspectionTypeIDs": None,
        "PageNumber": page_number,
        "PageSize": page_size,
        "SortBy": sort_by,
        "SortAscending": sort_ascending,
    }

"""Auth + request headers for the EnerGov CSS JSON API.

Recon (2026-05-26) confirmed the public search API answers ANONYMOUSLY — a
logged-out browser POSTs search/search with NO Authorization header and gets
HTTP 200 + full results. So the default path needs no token.

Fallback only: if the portal ever starts rejecting anonymous requests, set
SCA_BEARER_TOKEN in the environment to a Bearer token copied from a logged-in
browser session. This module never performs a login — the user authenticates;
we merely attach whatever token they provide.
"""

from __future__ import annotations

import os

from .config import (CONTENT_TYPE, TENANT_CULTURE, TENANT_ID, TENANT_NAME,
                     TENANT_URL, USER_AGENT)


def bearer_token() -> str | None:
    """Return the optional fallback Bearer token from the environment, if set."""
    tok = (os.environ.get("SCA_BEARER_TOKEN") or "").strip()
    return tok or None


def search_headers(include_tenant: bool = True) -> dict:
    """Headers for the search POST. Anonymous by default; adds Authorization only
    if SCA_BEARER_TOKEN is set (fallback).

    `tenantId` is ALWAYS sent — the spike proved the API 500s without it.
    `include_tenant` toggles the three non-required tenant headers
    (tenantName / Tyler-TenantUrl / Tyler-Tenant-Culture), sent for SPA parity.
    """
    headers = {
        "Accept": "application/json, text/plain, */*",
        "Content-Type": CONTENT_TYPE,
        "tenantId": TENANT_ID,             # REQUIRED
        "User-Agent": USER_AGENT,
        "Origin": "https://sancarlosca-energovweb.tylerhost.net",
        "Referer": "https://sancarlosca-energovweb.tylerhost.net/apps/selfservice",
    }
    if include_tenant:
        headers.update({
            "tenantName": TENANT_NAME,
            "Tyler-TenantUrl": TENANT_URL,
            "Tyler-Tenant-Culture": TENANT_CULTURE,
        })
    tok = bearer_token()
    if tok:
        headers["Authorization"] = tok if tok.lower().startswith("bearer ") \
            else f"Bearer {tok}"
    return headers

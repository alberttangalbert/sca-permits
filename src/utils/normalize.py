"""Address normalization helpers — used by step 1 + (future) clustering.

Ported from cu-permits; the only change is the city tail regex (CUPERTINO/950xx
-> SAN CARLOS/940xx). San Carlos EnerGov returns addresses like:
  "825 INDUSTRIAL RD SAN CARLOS CA 94070"
  "Unit: Suite B San Carlos CA 94070"
  "2017 GREENWOOD AVE SAN CARLOS CA 94070"

normalize_address() reduces these to a canonical form for grouping records that
refer to the same physical site.

CAVEAT (handoff/detail-api.md): stripping the unit OVER-COLLAPSES multifamily
buildings. Keep the unit in a SEPARATE column (split_address() / address_unit)
and prefer EnerGov related-records over address clustering when available.
"""

from __future__ import annotations

import re


_WHITESPACE = re.compile(r"\s+")
# City tail: ", SAN CARLOS CA 94070-1234" / " SAN CARLOS CA 94070" (comma optional).
_CITY_TAIL = re.compile(
    r",?\s*SAN\s+CARLOS(\s*[,]?\s*(CA|CALIFORNIA))?"
    r"(\s*,?\s*9\d{4}(-\d{4})?)?$",
    re.IGNORECASE)
_TRAILING_STAR = re.compile(r"\s*\*+\s*$")
# Unit qualifier: ", G" / ", 29" / ", #5" / ", APT B" — short trailing token.
_UNIT_TAIL = re.compile(
    r",\s*((unit|apt|suite|ste|#)\s*)?[\w\d#]{1,5}\s*$",
    re.IGNORECASE)


def normalize_address(raw: str | None) -> str:
    """Return a canonical uppercased address suitable for grouping.

    Examples:
      "825 INDUSTRIAL RD SAN CARLOS CA 94070"   -> "825 INDUSTRIAL RD"
      "2017 GREENWOOD AVE SAN CARLOS CA 94070"  -> "2017 GREENWOOD AVE"
      None / ""                                 -> ""
    """
    return split_address(raw)[0]


def split_address(raw: str | None) -> tuple[str, str]:
    """Return (canonical_address, unit). The unit is kept separate so that
    multifamily buildings are NOT collapsed into one cluster.

    Examples:
      "1460 ALAMEDA, Apt 29, San Carlos CA 94070" -> ("1460 ALAMEDA", "APT 29")
      "825 INDUSTRIAL RD SAN CARLOS CA 94070"     -> ("825 INDUSTRIAL RD", "")
    """
    if not raw:
        return "", ""
    s = str(raw).strip().upper()
    s = _TRAILING_STAR.sub("", s)
    s = _CITY_TAIL.sub("", s).strip().strip(",").strip()
    unit = ""
    m = _UNIT_TAIL.search(s)
    if m:
        unit = _WHITESPACE.sub(" ", m.group(0).lstrip(", ").strip()).strip()
        s = s[:m.start()].strip().strip(",").strip()
    s = _WHITESPACE.sub(" ", s)
    return s, unit

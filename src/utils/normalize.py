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
# EnerGov's primary unit pattern: "<street> Unit: <unit text>". The unit text
# runs to end-of-string and may be longer than the comma-form (e.g. 'APT # 304').
# Detected 2026-05-29: 3,594 of 3,616 'Unit:' addresses were missing extraction
# because they don't use the comma-form. Convert to the comma-form BEFORE the
# comma-based extractor runs, so both pipelines yield the same (norm, unit).
_ENERGOV_UNIT = re.compile(r"\s+UNIT\s*:\s*", re.IGNORECASE)
# Comma-form unit qualifier: ", G" / ", 29" / ", #5" / ", APT B" / ", APT # 304"
# / ", SUITE B" / ", APT. 2" / ", C & D" / ", # H & J-K". The content runs to
# end of string and preserves the marker word so "APT # 304" stays "APT # 304"
# (vs. just "304"), since the marker is part of the canonical unit label. The
# pattern requires at least one alphanumeric/# character so an empty ", "
# doesn't false-match. '&' is included because multi-unit commercial spaces
# at the same address use it ("C & D") and ~50 SC addresses follow that form.
_UNIT_TAIL = re.compile(
    r",\s*([\w#][\w#./\-& ]*?)\s*$",
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
    # EnerGov's "Unit:" separator -> comma-form, so one extractor handles both.
    s = _ENERGOV_UNIT.sub(", ", s)
    unit = ""
    m = _UNIT_TAIL.search(s)
    if m:
        unit = _WHITESPACE.sub(" ", m.group(1).strip()).strip()
        s = s[:m.start()].strip().strip(",").strip()
    s = _WHITESPACE.sub(" ", s)
    return s, unit

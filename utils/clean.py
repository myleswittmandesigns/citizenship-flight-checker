"""
Flight record deduplication and data cleaning.

Two emails about the same flight (booking confirmation + check-in reminder,
or a confirmation forwarded by an employer) will produce two partially-
overlapping records. This module:

  1. Normalises fields (flight numbers, PNRs, airport codes) so that
     formatting differences don't prevent matching.
  2. Identifies duplicate records using three independent fingerprints —
     any one match is sufficient to call two records the same flight.
  3. Merges each duplicate group into the single most-complete record
     rather than discarding information.

Fingerprints (any match → same flight)
───────────────────────────────────────
  FP-A  normalised flight number + departure date
  FP-B  departure airport + arrival airport + departure date
  FP-C  booking reference (PNR) + departure airport

Connected components are resolved with union-find so that transitive
matches are handled correctly (A matches B, B matches C → all three merge).
"""

import re


# ── Normalization helpers ──────────────────────────────────────────────────────

def _norm_flight_num(v: str | None) -> str | None:
    """'UA 123', 'ua123', 'UA-123' → 'UA123'"""
    if not v:
        return None
    s = re.sub(r"[\s\-_]", "", v.strip()).upper()
    return s if s else None


def _norm_pnr(v: str | None) -> str | None:
    """Strip whitespace and uppercase the PNR."""
    if not v:
        return None
    s = re.sub(r"\s+", "", v.strip()).upper()
    return s if len(s) >= 4 else None   # ignore noise like "OK"


def _norm_iata(v: str | None) -> str | None:
    if not v:
        return None
    s = v.strip().upper()
    return s if re.match(r"^[A-Z]{3}$", s) else None


# ── Duplicate detection ───────────────────────────────────────────────────────

def _fingerprints(f: dict) -> set[str]:
    """Return the set of non-trivial fingerprints for a flight record."""
    fps: set[str] = set()

    fn   = _norm_flight_num(f.get("flight_number"))
    date = (f.get("departure_date") or "").strip()
    dep  = _norm_iata(f.get("departure_airport"))
    arr  = _norm_iata(f.get("arrival_airport"))
    pnr  = _norm_pnr(f.get("booking_reference"))

    # FP-A: flight number + date (strongest — uniquely identifies a flight)
    if fn and date:
        fps.add(f"FN:{fn}|{date}")

    # FP-B: dep airport + arr airport + date
    if dep and arr and date:
        fps.add(f"RT:{dep}|{arr}|{date}")

    # FP-C: booking reference + departure airport
    # (PNR alone is not enough — same PNR can appear on multiple segments
    #  of a multi-leg itinerary with different dep airports)
    if pnr and dep:
        fps.add(f"PNR:{pnr}|{dep}")

    return fps


def _same_flight(a: dict, b: dict) -> bool:
    """True if records a and b share at least one fingerprint."""
    return bool(_fingerprints(a) & _fingerprints(b))


# ── Union-find ────────────────────────────────────────────────────────────────

def _make_groups(flights: list[dict]) -> list[list[int]]:
    """Return lists of indices that belong to the same flight (connected components)."""
    n = len(flights)
    parent = list(range(n))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    def union(i: int, j: int) -> None:
        parent[find(i)] = find(j)

    for i in range(n):
        for j in range(i + 1, n):
            if _same_flight(flights[i], flights[j]):
                union(i, j)

    groups: dict[int, list[int]] = {}
    for i in range(n):
        root = find(i)
        groups.setdefault(root, []).append(i)

    return list(groups.values())


# ── Merge ─────────────────────────────────────────────────────────────────────

_FIELDS = [
    "airline", "flight_number",
    "departure_airport", "departure_city", "departure_country",
    "arrival_airport",  "arrival_city",   "arrival_country",
    "departure_date", "arrival_date", "booking_reference",
]


def _merge_group(records: list[dict]) -> dict:
    """Merge a group of duplicate records into one, maximising completeness.

    Strategy per field:
      - Take the first non-null value across all records in the group.
      - For flight_number and booking_reference, prefer the normalised
        (no-space) form if multiple variants exist.
    """
    if len(records) == 1:
        return records[0]

    merged: dict = {}
    for field in _FIELDS:
        values = [r[field] for r in records if r.get(field)]
        if not values:
            merged[field] = None
            continue

        if field == "flight_number":
            # Prefer the normalised form (no spaces)
            normed = [_norm_flight_num(v) for v in values if _norm_flight_num(v)]
            merged[field] = normed[0] if normed else values[0]
        elif field == "booking_reference":
            normed = [_norm_pnr(v) for v in values if _norm_pnr(v)]
            merged[field] = normed[0] if normed else values[0]
        else:
            merged[field] = values[0]

    return merged


# ── Public API ────────────────────────────────────────────────────────────────

def deduplicate_flights(flights: list[dict]) -> list[dict]:
    """Remove duplicate flight records and merge incomplete duplicates.

    Two records are considered duplicates if they share any of:
      - Same normalised flight number on the same date
      - Same departure + arrival airport on the same date
      - Same booking reference departing from the same airport

    Duplicates are merged into the single most-complete record.

    Args:
        flights: List of flight dicts (unsorted, may contain duplicates).

    Returns:
        Deduplicated list sorted chronologically by departure date.
    """
    if len(flights) <= 1:
        return flights

    groups  = _make_groups(flights)
    result  = [_merge_group([flights[i] for i in grp]) for grp in groups]
    result.sort(key=lambda f: f.get("departure_date") or "9999-12-31")
    return result

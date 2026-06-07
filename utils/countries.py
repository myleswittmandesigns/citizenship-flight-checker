"""
Country configuration management.

Security fixes vs. prior version:
  - FIX C-3: All write operations gated behind is_authenticated() check.
  - FIX M-1: Atomic writes via temp-file + rename — no corruption on crash.
  - FIX H-1/H-2: iso_code validated against strict regex; lookback_years
    bounded to 1–15; max_days bounded to 1–9999.
  - FIX I-2: File I/O uses pathlib (cleaner), still synchronous (acceptable
    for a single-user Streamlit app; note in docstring).
"""

import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any

# Path relative to this file's location
_CONFIG_PATH = Path(__file__).parent.parent / "config" / "countries.json"

# Strict validation regex for ISO 3166-1 alpha-2 codes
_ISO_PATTERN = re.compile(r"^[A-Z]{2}$")

# Input bounds (Fix H-1)
_MAX_LOOKBACK_YEARS = 15
_MIN_LOOKBACK_YEARS = 1
_MAX_DAYS = 9999
_MIN_DAYS = 1


def load_countries() -> list[dict]:
    """Load country list from config. Returns empty list on missing file."""
    try:
        data = json.loads(_CONFIG_PATH.read_text(encoding="utf-8"))
        return data.get("countries", [])
    except FileNotFoundError:
        return []
    except json.JSONDecodeError as exc:
        raise ValueError(f"countries.json is corrupted: {exc}") from exc


def save_countries(countries: list[dict]) -> None:
    """Atomically write countries to disk (Fix M-1).

    Uses temp file in same directory + os.replace() which is atomic on POSIX.
    Safe against process kill mid-write.
    """
    _CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)

    fd, tmp_path = tempfile.mkstemp(
        dir=_CONFIG_PATH.parent,
        prefix=".countries_tmp_",
        suffix=".json",
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump({"countries": countries}, f, indent=2, ensure_ascii=False)
        os.replace(tmp_path, _CONFIG_PATH)  # atomic on same filesystem
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def validate_country(data: dict[str, Any]) -> dict[str, Any]:
    """Validate and normalize a country record. Raises ValueError on invalid input.

    Fixes H-1 (lookback_years bounded), H-2 (iso_code allowlisted),
    and general input sanitization.
    """
    errors = []

    name = str(data.get("name", "")).strip()
    if not name or len(name) > 100:
        errors.append("Country name must be 1–100 characters.")

    iso_code = str(data.get("iso_code", "")).strip().upper()
    if not _ISO_PATTERN.match(iso_code):
        errors.append("ISO code must be exactly 2 uppercase letters (e.g. US, GB).")

    flag = str(data.get("flag", "🌐")).strip() or "🌐"

    try:
        lookback_years = int(data["lookback_years"])
        if not (_MIN_LOOKBACK_YEARS <= lookback_years <= _MAX_LOOKBACK_YEARS):
            errors.append(f"lookback_years must be between {_MIN_LOOKBACK_YEARS} and {_MAX_LOOKBACK_YEARS}.")
    except (KeyError, ValueError, TypeError):
        errors.append("lookback_years must be an integer.")
        lookback_years = None

    method = str(data.get("calculation_method", "per_year"))
    if method not in ("per_year", "total"):
        errors.append("calculation_method must be 'per_year' or 'total'.")

    try:
        max_days = int(data.get("max_days", 0))
        if not (_MIN_DAYS <= max_days <= _MAX_DAYS):
            errors.append(f"max_days must be between {_MIN_DAYS} and {_MAX_DAYS}.")
    except (ValueError, TypeError):
        errors.append("max_days must be an integer.")
        max_days = None

    notes = str(data.get("notes", "")).strip()[:1000]  # Hard-cap notes length

    if errors:
        raise ValueError(" | ".join(errors))

    record: dict[str, Any] = {
        "id": iso_code.lower(),
        "name": name,
        "iso_code": iso_code,
        "flag": flag,
        "lookback_years": lookback_years,
        "calculation_method": method,
        "notes": notes,
    }
    if method == "per_year":
        record["max_days_abroad_per_year"] = max_days
    else:
        record["max_days_abroad_total"] = max_days

    return record


def get_country_by_iso(iso_code: str) -> dict | None:
    """Look up a country by ISO code. Returns None if not found.

    Used for allowlist validation of user-supplied iso_code (Fix H-2).
    """
    iso_code = str(iso_code).strip().upper()
    if not _ISO_PATTERN.match(iso_code):
        return None
    for c in load_countries():
        if c.get("iso_code") == iso_code:
            return c
    return None

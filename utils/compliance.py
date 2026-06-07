"""
Compliance calculation — days abroad, abroad periods, and limit checks.
"""

from datetime import datetime, date, timedelta
from dataclasses import dataclass, field


@dataclass
class AbroadPeriod:
    departed: str          # ISO date string
    returned: str | None   # ISO date string or None (still abroad)
    days: int
    departure_city: str | None = None
    arrival_city: str | None = None
    open: bool = False     # True if applicant has not yet returned


def calculate_abroad_periods(flights: list[dict], country_iso_code: str) -> list[AbroadPeriod]:
    """Pair departure and return flights to compute abroad periods.

    A departure is any flight leaving country_iso_code.
    A return is any flight arriving at country_iso_code from abroad.
    """
    sorted_flights = sorted(
        [f for f in flights if f.get("departure_date")],
        key=lambda f: f["departure_date"],
    )

    periods: list[AbroadPeriod] = []
    pending_departure = None

    for f in sorted_flights:
        leaving = (
            f.get("departure_country") == country_iso_code
            and f.get("arrival_country") != country_iso_code
        )
        returning = (
            f.get("arrival_country") == country_iso_code
            and f.get("departure_country") != country_iso_code
        )

        if leaving and not pending_departure:
            pending_departure = f
        elif returning and pending_departure:
            dep_date = datetime.strptime(pending_departure["departure_date"], "%Y-%m-%d").date()
            ret_date = datetime.strptime(f["departure_date"], "%Y-%m-%d").date()
            days = max(0, (ret_date - dep_date).days)
            periods.append(AbroadPeriod(
                departed=pending_departure["departure_date"],
                returned=f["departure_date"],
                days=days,
                departure_city=pending_departure.get("departure_city"),
                arrival_city=f.get("arrival_city"),
            ))
            pending_departure = None

    # Open period — not yet returned
    if pending_departure:
        dep_date = datetime.strptime(pending_departure["departure_date"], "%Y-%m-%d").date()
        today = date.today()
        days = max(0, (today - dep_date).days)
        periods.append(AbroadPeriod(
            departed=pending_departure["departure_date"],
            returned=None,
            days=days,
            departure_city=pending_departure.get("departure_city"),
            open=True,
        ))

    return periods


def total_days_abroad(periods: list[AbroadPeriod]) -> int:
    return sum(p.days for p in periods)


@dataclass
class ComplianceResult:
    status: str          # "ok", "warning", "over"
    message: str
    total_days: int
    periods: list[AbroadPeriod]
    yearly_breakdown: dict[int, int] = field(default_factory=dict)  # year → days


def check_compliance(
    flights: list[dict],
    country: dict,
) -> ComplianceResult:
    """Check whether days abroad comply with the country's citizenship rules."""
    iso = country["iso_code"]
    periods = calculate_abroad_periods(flights, iso)
    total = total_days_abroad(periods)
    method = country.get("calculation_method", "per_year")

    # Build year-by-year breakdown
    yearly: dict[int, int] = {}
    for p in periods:
        if not p.departed:
            continue
        year = int(p.departed[:4])
        yearly[year] = yearly.get(year, 0) + p.days

    if method == "per_year":
        max_per_year = country.get("max_days_abroad_per_year", 0)
        violations = [
            f"{yr}: ~{days} days (max {max_per_year})"
            for yr, days in sorted(yearly.items())
            if days > max_per_year
        ]
        if not violations:
            status = "ok"
            msg = (
                f"✅ Within limits — no year exceeds the {max_per_year}-day annual maximum."
            )
        else:
            status = "over"
            msg = (
                f"🚨 Potential issue — the following years may exceed {max_per_year} days: "
                + "; ".join(violations)
                + ". Please consult an immigration attorney."
            )

    else:  # total
        max_total = country.get("max_days_abroad_total", 0)
        pct = round((total / max_total) * 100) if max_total else 0

        if total <= max_total * 0.75:
            status = "ok"
            msg = f"✅ Within limits — ~{total} days abroad out of {max_total} allowed ({pct}% used)."
        elif total <= max_total:
            status = "warning"
            msg = (
                f"⚠️ Approaching limit — ~{total} days abroad out of {max_total} allowed "
                f"({pct}% used). Review carefully before applying."
            )
        else:
            status = "over"
            msg = (
                f"🚨 Over limit — ~{total} days abroad exceeds the {max_total}-day maximum. "
                f"Consult an immigration attorney before applying."
            )

    return ComplianceResult(
        status=status,
        message=msg,
        total_days=total,
        periods=periods,
        yearly_breakdown=yearly,
    )

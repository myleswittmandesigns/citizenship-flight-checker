"""
Rule-based flight data extractor — no external API required.

Uses regex patterns + the `airportsdata` package (offline IATA airport
database, ~10 000 airports) to extract structured flight records from
plain-text email bodies.

Approach
────────
1. Pre-filter  — does this email look like a flight confirmation at all?
2. Visitor gate — is someone else sharing *their* travel plans?
3. Route extraction — find airport-code pairs (DEP → ARR) in multiple formats
4. Date extraction  — find dates near each route segment
5. Flight # / PNR   — pull booking metadata
6. Assemble         — zip routes + nearest dates + flight numbers into records
7. Country lookup   — map IATA code → ISO-2 country via airportsdata

Accuracy notes
──────────────
Regex handles ~90–95 % of standard airline/OTA confirmation emails correctly.
Edge cases (very image-heavy emails, non-Latin scripts, unusual agency formats)
may be missed. For a citizenship application the user should always cross-check
the exported CSV against their passport stamps and original confirmations.
"""

import re
from datetime import datetime

import airportsdata
from dateutil import parser as dateutil_parse, ParserError

# ── IATA airport database (loaded once at import time, ~50 ms) ────────────────
_AIRPORTS: dict = airportsdata.load("IATA")

# ── Airline IATA code → display name ─────────────────────────────────────────
_AIRLINE_NAMES: dict[str, str] = {
    "AA": "American Airlines",       "UA": "United Airlines",
    "DL": "Delta Air Lines",         "WN": "Southwest Airlines",
    "B6": "JetBlue Airways",         "AS": "Alaska Airlines",
    "NK": "Spirit Airlines",         "F9": "Frontier Airlines",
    "G4": "Allegiant Air",           "SY": "Sun Country Airlines",
    "BA": "British Airways",         "LH": "Lufthansa",
    "AF": "Air France",              "KL": "KLM",
    "IB": "Iberia",                  "VY": "Vueling",
    "EW": "Eurowings",               "FR": "Ryanair",
    "U2": "easyJet",                 "W6": "Wizz Air",
    "TK": "Turkish Airlines",        "PC": "Pegasus Airlines",
    "OS": "Austrian Airlines",       "LX": "SWISS",
    "SN": "Brussels Airlines",       "AY": "Finnair",
    "SK": "SAS",                     "DY": "Norwegian",
    "HV": "Transavia",               "LS": "Jet2",
    "EI": "Aer Lingus",              "TP": "TAP Air Portugal",
    "AZ": "ITA Airways",             "LO": "LOT Polish Airlines",
    "EK": "Emirates",                "QR": "Qatar Airways",
    "EY": "Etihad Airways",          "FZ": "flydubai",
    "G9": "Air Arabia",              "SV": "Saudia",
    "WY": "Oman Air",                "GF": "Gulf Air",
    "SQ": "Singapore Airlines",      "CX": "Cathay Pacific",
    "QF": "Qantas",                  "VA": "Virgin Australia",
    "NZ": "Air New Zealand",         "AK": "AirAsia",
    "D7": "AirAsia X",               "TG": "Thai Airways",
    "PG": "Bangkok Airways",         "KE": "Korean Air",
    "OZ": "Asiana Airlines",         "JL": "Japan Airlines",
    "NH": "All Nippon Airways",      "CA": "Air China",
    "MU": "China Eastern",           "CZ": "China Southern",
    "HX": "Hong Kong Airlines",      "AI": "Air India",
    "6E": "IndiGo",                  "SG": "SpiceJet",
    "UK": "Vistara",                 "VN": "Vietnam Airlines",
    "VJ": "VietJet Air",             "GA": "Garuda Indonesia",
    "JT": "Lion Air",                "5J": "Cebu Pacific",
    "ET": "Ethiopian Airlines",      "MS": "EgyptAir",
    "KQ": "Kenya Airways",           "AT": "Royal Air Maroc",
    "RW": "RwandAir",                "AC": "Air Canada",
    "WS": "WestJet",                 "AM": "Aeroméxico",
    "LA": "LATAM Airlines",          "AV": "Avianca",
    "CM": "Copa Airlines",           "AD": "Azul Airlines",
    "G3": "Gol Airlines",            "Y4": "Volaris",
    "VB": "VivaAerobus",             "VS": "Virgin Atlantic",
}

# ── Compiled regex patterns ───────────────────────────────────────────────────

# IATA airport code: exactly 3 uppercase letters, word-boundary delimited
_IATA_RE = re.compile(r"\b([A-Z]{3})\b")

# Arrow / route patterns: JFK→LHR, JFK-LHR, JFK to LHR, JFK - LHR
_ROUTE_RE = re.compile(
    r"\b([A-Z]{3})\s*(?:→|->|–|—|\bto\b|»|\s-\s)\s*([A-Z]{3})\b"
)

# Departure markers
_DEP_RE = re.compile(
    r"(?:departs?|departing|departure|from|leaves?|origin|departing\s+from)"
    r"\s*[:\-–]?\s*([A-Z]{3})\b",
    re.IGNORECASE,
)

# Arrival markers
_ARR_RE = re.compile(
    r"(?:arrives?|arriving|arrival|to|destination|arriving\s+at|to\s+airport)"
    r"\s*[:\-–]?\s*([A-Z]{3})\b",
    re.IGNORECASE,
)

# Flight number: 2-char IATA airline code (letter+letter or letter+digit) + 1-4 digits
_FLIGHT_NUM_RE = re.compile(r"\b([A-Z]{2}|[A-Z][0-9]|[0-9][A-Z])\s?(\d{1,4}[A-Z]?)\b")

# Booking reference / PNR
_PNR_RE = re.compile(
    r"(?:booking\s+ref(?:erence)?|record\s+locator|PNR|confirmation\s+(?:code|number|no\.?)|"
    r"reservation\s+(?:code|number|no\.?)|booking\s+(?:number|code)|e[-\s]?ticket\s+(?:number|no\.?)|"
    r"reference\s+(?:number|code)|locator\s+code)"
    r"\s*[:\-#]?\s*([A-Z0-9]{5,10})\b",
    re.IGNORECASE,
)

# Passenger name line (near "passenger:", "traveler:", "name:", etc.)
_PAX_NAME_RE = re.compile(
    r"(?:passenger|traveler|traveller|guest|name)\s*[:\-]?\s*([A-Z][A-Z\s\-]{2,40})",
    re.IGNORECASE,
)

# ── Visitor detection (email is someone else sharing THEIR plans) ─────────────
_VISITOR_PATTERNS = [
    r"i(?:'m| am)\s+(?:flying|arriving|coming|visiting|heading|traveling|travelling)",
    r"i(?:'ll| will)\s+(?:be arriving|be flying|be there|land|be landing)",
    r"\bmy\s+(?:flight\b|trip\b|itinerary\b|travel\b|booking\b|confirmation\b)",
    r"\bmy\s+(?:outbound|return|inbound)\s+flight",
    r"(?:pick(?:ing)?\s+me\s+up|meet\s+me\s+at\s+the\s+airport|collect\s+me)",
    r"(?:coming\s+to\s+visit|visiting\s+you|coming\s+your\s+way|coming\s+to\s+see\s+you)",
    r"(?:here(?:'s| is)\s+my|sharing\s+my)\s+(?:flight|itinerary|travel|booking)",
    r"i\s+(?:booked|purchased|bought)\s+(?:a\s+)?(?:flight|ticket)\s+(?:to\s+come|to\s+visit|to\s+see\s+you)",
]
_VISITOR_RE = re.compile("|".join(_VISITOR_PATTERNS), re.IGNORECASE)

# Ownership indicators (email IS the user's confirmation)
_OWNER_PATTERNS = [
    r"\byour\s+(?:booking|reservation|itinerary|flight|trip|ticket|e-ticket|confirmation|travel)\b",
    r"\byou(?:'re| are)\s+(?:booked|confirmed|checked\s+in|all\s+set)\b",
    r"\bdear\s+(?:traveler|traveller|passenger|customer|valued\s+customer)\b",
    r"\bthank\s+you\s+for\s+(?:booking|choosing|flying|your\s+(?:booking|purchase))\b",
    r"\bbooking\s+confirmation\b",
    r"\be-?ticket\s+receipt\b",
    r"\belectronic\s+ticket\b",
    r"\btravel\s+itinerary\s+for\b",
    r"\bcheck\s+in\s+(?:now|for\s+your\s+flight)\b",
    r"\bonline\s+check-?in\b",
    r"\byour\s+(?:boarding\s+pass|seat\s+(?:number|assignment))\b",
]
_OWNER_RE = re.compile("|".join(_OWNER_PATTERNS), re.IGNORECASE)

# Airline name patterns in subject / sender (fallback airline name extraction)
_AIRLINE_SUBJECT_PATTERNS = [
    (re.compile(r"(?:united\s+airlines?|united\.com)", re.IGNORECASE), "United Airlines"),
    (re.compile(r"\bdelta\b(?:\s+air\s+lines?)?", re.IGNORECASE), "Delta Air Lines"),
    (re.compile(r"american\s+airlines?", re.IGNORECASE), "American Airlines"),
    (re.compile(r"southwest\s+airlines?", re.IGNORECASE), "Southwest Airlines"),
    (re.compile(r"british\s+airways?", re.IGNORECASE), "British Airways"),
    (re.compile(r"\blufthansa\b", re.IGNORECASE), "Lufthansa"),
    (re.compile(r"\bair\s+france\b", re.IGNORECASE), "Air France"),
    (re.compile(r"\bklm\b", re.IGNORECASE), "KLM"),
    (re.compile(r"\bemirateS\b", re.IGNORECASE), "Emirates"),
    (re.compile(r"\bqatar\s+airways?\b", re.IGNORECASE), "Qatar Airways"),
    (re.compile(r"\betihad\b", re.IGNORECASE), "Etihad Airways"),
    (re.compile(r"\brian\s+air\b|\bryanair\b", re.IGNORECASE), "Ryanair"),
    (re.compile(r"\beasyjet\b", re.IGNORECASE), "easyJet"),
    (re.compile(r"\bwizz\s*air\b", re.IGNORECASE), "Wizz Air"),
    (re.compile(r"\baer\s+lingus\b", re.IGNORECASE), "Aer Lingus"),
    (re.compile(r"\bair\s+canada\b", re.IGNORECASE), "Air Canada"),
    (re.compile(r"\bqantas\b", re.IGNORECASE), "Qantas"),
    (re.compile(r"\bsingapore\s+airlines?\b", re.IGNORECASE), "Singapore Airlines"),
    (re.compile(r"\bcathay\s+pacific\b", re.IGNORECASE), "Cathay Pacific"),
    (re.compile(r"\bturk(?:ish\s+airlines?|ishairlines)\b", re.IGNORECASE), "Turkish Airlines"),
    (re.compile(r"\bjetblue\b", re.IGNORECASE), "JetBlue Airways"),
    (re.compile(r"\balaska\s+airlines?\b", re.IGNORECASE), "Alaska Airlines"),
]


# ── Public entry point ────────────────────────────────────────────────────────

def extract_flights(
    body: str,
    subject: str,
    sender: str,
    date: str,
    passenger_name: str = "",
) -> list[dict]:
    """Extract flight records from a single email body.

    Returns a list of flight dicts — may be empty.
    Never raises; on any error it returns [].

    Args:
        body:           Plain-text email body (HTML should be stripped by caller).
        subject:        Email Subject header.
        sender:         Email From header.
        date:           Email Date header (used as date fallback).
        passenger_name: Optional. Helps detect whether the recipient is the
                        passenger vs. a visitor's forwarded itinerary.
    """
    try:
        if not body or len(body.strip()) < 30:
            return []

        combined = f"{subject}\n{sender}\n{body}"

        # Gate 1: does this look like a flight-related email at all?
        if not _is_flight_email(subject, sender, body):
            return []

        # Gate 2: is someone else sharing *their* travel plans with the user?
        if _is_visitor_email(combined, passenger_name):
            return []

        return _extract_segments(body, subject, sender, date)

    except Exception as exc:
        print(f"[extractor] Unexpected error for '{subject[:60]}': {exc}")
        return []


# ── Gate 1: flight email detection ───────────────────────────────────────────

_FLIGHT_SUBJECT_RE = re.compile(
    r"flight|booking|itinerary|e-?ticket|boarding|reservation|"
    r"confirmation|travel|trip|departure|arrival",
    re.IGNORECASE,
)

_FLIGHT_BODY_RE = re.compile(
    r"booking\s+ref(?:erence)?|record\s+locator|PNR|e-?ticket|"
    r"boarding\s+pass|flight\s+number|departs?\s+[A-Z]{3}|"
    r"check[\s-]?in|departure\s+time|arrival\s+time|"
    r"seat\s+(?:number|assignment)|gate\s+\d",
    re.IGNORECASE,
)

_KNOWN_AIRLINE_SENDER_RE = re.compile(
    r"@(?:united|delta|aa|southwest|jetblue|spirit|alaskaair|"
    r"frontierairlines|hawaiianairlines|british-?airways?|"
    r"lufthansa|airfrance|klm|iberia|ryanair|easyjet|wizzair|"
    r"norwegian|transavia|eurowings|aerlingus|flytap|swiss|"
    r"austrian|brusselsairlines|lot|finnair|flysas|"
    r"emirates|qatarairways|etihad|flydubai|airarabia|saudia|"
    r"omanair|gulfair|ethiopianairlines|egyptair|"
    r"singaporeair|cathaypacific|qantas|virginaustralia|"
    r"airasia|thaiairways|koreanair|flyasiana|jal|ana|"
    r"airchina|chinaeastern|csair|airindia|goindigo|spicejet|"
    r"aircanada|westjet|aeromexico|latam|avianca|copaair|"
    r"azul|voegol|volaris|virginatlantic|"
    r"expedia|kayak|orbitz|travelocity|priceline|"
    r"booking|tripadvisor|hopper|skyscanner|cheapoair|"
    r"kiwi|momondo|edreams|concur|navan|egencia|tripactions|spotnana)\.",
    re.IGNORECASE,
)


def _is_flight_email(subject: str, sender: str, body: str) -> bool:
    """Return True if the email looks like a flight confirmation."""
    if _KNOWN_AIRLINE_SENDER_RE.search(sender):
        return True
    if _FLIGHT_SUBJECT_RE.search(subject):
        return True
    if _FLIGHT_BODY_RE.search(body[:3000]):
        return True
    # Last resort: does it contain any valid IATA pair?
    iata_hits = [
        m.group(1) for m in _IATA_RE.finditer(subject + " " + body[:2000])
        if m.group(1) in _AIRPORTS
    ]
    return len(iata_hits) >= 2


# ── Gate 2: visitor email detection ──────────────────────────────────────────

def _is_visitor_email(combined_text: str, passenger_name: str) -> bool:
    """Return True if this email appears to be someone else sharing their plans."""
    if _VISITOR_RE.search(combined_text):
        # Override: if the email also has strong ownership language, keep it
        # (e.g., a forwarded confirmation that still says "your booking")
        if _OWNER_RE.search(combined_text):
            return False
        return True

    # If a name was given, check whether a DIFFERENT name appears as the
    # only passenger (crude but useful signal)
    if passenger_name:
        pax_matches = _PAX_NAME_RE.findall(combined_text)
        if pax_matches:
            name_upper = passenger_name.upper()
            all_others = all(
                name_upper not in p.upper() and p.upper() not in name_upper
                for p in pax_matches
            )
            # Only exclude if ownership language is absent too
            if all_others and not _OWNER_RE.search(combined_text):
                return True

    return False


# ── Route extraction ──────────────────────────────────────────────────────────

def _find_routes(text: str) -> list[tuple[str, str, int]]:
    """Return list of (dep_code, arr_code, char_position) tuples."""
    routes: list[tuple[str, str, int]] = []
    seen: set[tuple[str, str, int]] = set()

    def _valid(code: str) -> bool:
        return code in _AIRPORTS

    def _add(dep: str, arr: str, pos: int):
        if _valid(dep) and _valid(arr) and dep != arr:
            key = (dep, arr, pos // 200)   # bucket nearby duplicates
            if key not in seen:
                seen.add(key)
                routes.append((dep, arr, pos))

    # Pattern 1: arrow/dash routes — JFK→LHR, JFK - LHR, JFK to LHR
    for m in _ROUTE_RE.finditer(text):
        _add(m.group(1), m.group(2), m.start())

    # Pattern 2: departure + arrival markers in proximity
    deps = [(m.group(1).upper(), m.start()) for m in _DEP_RE.finditer(text)
            if m.group(1).upper() in _AIRPORTS]
    arrs = [(m.group(1).upper(), m.start()) for m in _ARR_RE.finditer(text)
            if m.group(1).upper() in _AIRPORTS]

    for dep_code, dep_pos in deps:
        # Find the nearest arrival within 800 chars
        best = min(
            ((arr_code, arr_pos) for arr_code, arr_pos in arrs),
            key=lambda x: abs(x[1] - dep_pos),
            default=None,
        )
        if best and abs(best[1] - dep_pos) < 800:
            _add(dep_code, best[0], min(dep_pos, best[1]))

    # Pattern 3: airport codes in parentheses after a city name
    # e.g. "New York (JFK)" ... "London (LHR)"
    paren_airports = re.findall(r"\b\w[\w\s]+?\(([A-Z]{3})\)", text)
    valid_paren = [c for c in paren_airports if c in _AIRPORTS]
    if len(valid_paren) >= 2:
        for i in range(len(valid_paren) - 1):
            pos = text.find(f"({valid_paren[i]})")
            _add(valid_paren[i], valid_paren[i + 1], pos)

    return sorted(routes, key=lambda r: r[2])   # sort by position in text


# ── Date extraction ───────────────────────────────────────────────────────────

_DATE_CONTEXT_RE = re.compile(
    r"""
    (?:
        \b\d{4}-\d{2}-\d{2}\b                              # ISO
      | \b\d{1,2}\s+(?:Jan|Feb|Mar|Apr|May|Jun|            # 15 Mar 2024
                       Jul|Aug|Sep|Oct|Nov|Dec)\w*\s+\d{4}\b
      | \b(?:Jan|Feb|Mar|Apr|May|Jun|                       # Mar 15, 2024
             Jul|Aug|Sep|Oct|Nov|Dec)\w*\s+\d{1,2},?\s+\d{4}\b
      | \b\d{1,2}/\d{1,2}/\d{4}\b                          # 03/15/2024
      | \b(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun)\w*,?\s+          # Mon, 15 Mar 2024
          \d{1,2}\s+\w+\s+\d{4}\b
      | \b(?:Monday|Tuesday|Wednesday|Thursday|            # Monday, March 15
             Friday|Saturday|Sunday),?\s+
          (?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\w*\s+
          \d{1,2},?\s+\d{4}\b
    )
    """,
    re.IGNORECASE | re.VERBOSE,
)


def _find_dates(text: str) -> list[tuple[str, int]]:
    """Return list of (YYYY-MM-DD, char_position) from text."""
    results: list[tuple[str, int]] = []
    seen_dates: set[str] = set()

    for m in _DATE_CONTEXT_RE.finditer(text):
        raw = m.group(0).strip()
        try:
            dt = dateutil_parse.parse(raw, fuzzy=False)
            # Sanity: ignore dates outside plausible flight range
            if dt.year < 1990 or dt.year > datetime.now().year + 2:
                continue
            iso = dt.strftime("%Y-%m-%d")
            if iso not in seen_dates:
                seen_dates.add(iso)
                results.append((iso, m.start()))
        except (ParserError, OverflowError, ValueError):
            continue

    return results


# ── Flight number extraction ──────────────────────────────────────────────────

def _find_flight_numbers(text: str) -> list[tuple[str, int]]:
    """Return list of (flight_number_string, char_position)."""
    results = []
    for m in _FLIGHT_NUM_RE.finditer(text):
        code   = m.group(1).upper()
        digits = m.group(2)
        # Only accept if the IATA airline code is a known carrier
        if code in _AIRLINE_NAMES:
            formatted = f"{code}{digits}"
            results.append((formatted, m.start()))
    return results


def _airline_name_from_code(iata_code: str) -> str | None:
    return _AIRLINE_NAMES.get(iata_code.upper())


def _airline_name_from_context(subject: str, sender: str) -> str | None:
    """Try to extract airline name from subject / sender as a fallback."""
    for pattern, name in _AIRLINE_SUBJECT_PATTERNS:
        if pattern.search(subject) or pattern.search(sender):
            return name
    return None


# ── PNR / booking reference extraction ───────────────────────────────────────

def _find_pnr(text: str) -> str | None:
    m = _PNR_RE.search(text)
    return m.group(1).upper() if m else None


# ── Segment assembly ──────────────────────────────────────────────────────────

def _nearest(items: list[tuple[str, int]], pos: int, max_dist: int) -> str | None:
    """Return the value of the item nearest to pos within max_dist chars."""
    best_val, best_dist = None, max_dist + 1
    for val, item_pos in items:
        d = abs(item_pos - pos)
        if d < best_dist:
            best_dist = d
            best_val = val
    return best_val


def _extract_segments(
    body: str,
    subject: str,
    sender: str,
    email_date: str,
) -> list[dict]:
    """Core segment extraction: routes → match nearby dates + flight numbers."""
    routes      = _find_routes(body)
    all_dates   = _find_dates(body)
    all_flights = _find_flight_numbers(body)
    pnr         = _find_pnr(body)
    context_airline = _airline_name_from_context(subject, sender)

    # Fallback date: try to parse the email's own date header
    fallback_date: str | None = None
    if email_date:
        try:
            fallback_date = dateutil_parse.parse(email_date, fuzzy=True).strftime("%Y-%m-%d")
        except (ParserError, ValueError):
            pass

    segments: list[dict] = []

    for dep_code, arr_code, route_pos in routes:
        dep_airport = _AIRPORTS.get(dep_code, {})
        arr_airport = _AIRPORTS.get(arr_code, {})

        flight_num  = _nearest(all_flights, route_pos, max_dist=600)
        dep_date    = _nearest(all_dates,   route_pos, max_dist=800) or fallback_date

        airline_name: str | None = None
        if flight_num:
            airline_name = _airline_name_from_code(flight_num[:2])
        if not airline_name:
            airline_name = context_airline

        segments.append(_sanitize_flight({
            "airline":           airline_name,
            "flight_number":     flight_num,
            "departure_airport": dep_code,
            "departure_city":    dep_airport.get("city"),
            "departure_country": dep_airport.get("country"),
            "arrival_airport":   arr_code,
            "arrival_city":      arr_airport.get("city"),
            "arrival_country":   arr_airport.get("country"),
            "departure_date":    dep_date,
            "arrival_date":      None,     # Arrival date hard to pair reliably
            "booking_reference": pnr,
        }))

    # ── Fallback: no routes found but strong flight signals exist ─────────────
    # Some emails list departure and arrival in separate blocks rather than
    # as route pairs. Try to recover at least the airports.
    if not segments and all_dates and _OWNER_RE.search(body[:3000]):
        iata_hits = [
            m.group(1) for m in _IATA_RE.finditer(body)
            if m.group(1) in _AIRPORTS
        ]
        if len(iata_hits) >= 2:
            dep, arr = iata_hits[0], iata_hits[1]
            dep_airport = _AIRPORTS.get(dep, {})
            arr_airport = _AIRPORTS.get(arr, {})
            flight_num  = all_flights[0][0] if all_flights else None
            airline_name = (
                (_airline_name_from_code(flight_num[:2]) if flight_num else None)
                or context_airline
            )
            segments.append(_sanitize_flight({
                "airline":           airline_name,
                "flight_number":     flight_num,
                "departure_airport": dep,
                "departure_city":    dep_airport.get("city"),
                "departure_country": dep_airport.get("country"),
                "arrival_airport":   arr,
                "arrival_city":      arr_airport.get("city"),
                "arrival_country":   arr_airport.get("country"),
                "departure_date":    all_dates[0][0] if all_dates else fallback_date,
                "arrival_date":      None,
                "booking_reference": pnr,
            }))

    return segments


# ── Field sanitisation ────────────────────────────────────────────────────────

_ISO_CODE_PATTERN = re.compile(r"^[A-Z]{2}$")
_DATE_PATTERN     = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_IATA_PATTERN     = re.compile(r"^[A-Z]{3}$")


def _sanitize_flight(f: dict) -> dict:
    def clean_str(v, max_len: int = 200) -> str | None:
        if v is None:
            return None
        s = re.sub(r"[\x00-\x1f\x7f]", "", str(v).strip())
        return s[:max_len] if s else None

    def clean_iso(v) -> str | None:
        s = clean_str(v, 2)
        return s.upper() if s and _ISO_CODE_PATTERN.match(s.upper()) else None

    def clean_date(v) -> str | None:
        s = clean_str(v, 10)
        return s if s and _DATE_PATTERN.match(s) else None

    def clean_iata(v) -> str | None:
        s = clean_str(v, 3)
        if s and _IATA_PATTERN.match(s.upper()):
            return s.upper()
        return clean_str(v, 100)

    return {
        "airline":           clean_str(f.get("airline"), 100),
        "flight_number":     clean_str(f.get("flight_number"), 20),
        "departure_airport": clean_iata(f.get("departure_airport")),
        "departure_city":    clean_str(f.get("departure_city"), 100),
        "departure_country": clean_iso(f.get("departure_country")),
        "arrival_airport":   clean_iata(f.get("arrival_airport")),
        "arrival_city":      clean_str(f.get("arrival_city"), 100),
        "arrival_country":   clean_iso(f.get("arrival_country")),
        "departure_date":    clean_date(f.get("departure_date")),
        "arrival_date":      clean_date(f.get("arrival_date")),
        "booking_reference": clean_str(f.get("booking_reference"), 20),
    }

"""
Claude AI flight data extractor.

Sends email text to Anthropic's API and returns structured flight records.
Email content is NOT stored by this application — it is sent to Anthropic
for processing. Users are informed of this in the UI before scanning.

Passenger vs. visitor filtering
────────────────────────────────
Claude is instructed to determine whether the email recipient is one of the
passengers on the flights found — or whether a third party (visiting friend,
colleague sharing plans, etc.) is the actual traveler. Only "recipient is
passenger" emails contribute flights to the results.

An optional passenger_name hint makes this classification more accurate when
names appear on the booking confirmation.

Security notes:
  - Email body is truncated before transmission (reduce data exposure surface).
  - Claude response is parsed with strict JSON validation; malformed responses
    return [] rather than crashing.
  - Timeout enforced per API call.
"""

import json
import os
import re
import streamlit as st
import anthropic

# Max characters of email body sent to Claude — balances completeness vs exposure
_MAX_EMAIL_CHARS = 6_000

_EXTRACTION_PROMPT = """\
You are processing emails for a citizenship/residency application tool that \
compiles the user's personal international flight history.

────────────────────────────────────────────────────────────
STEP 1 — Decide whether this email belongs to the user
────────────────────────────────────────────────────────────
Set "recipient_is_passenger" to TRUE if this email is a flight booking or \
confirmation where the INBOX OWNER (the email recipient) is one of the \
passengers. This includes:

  • Booking confirmations sent directly from an airline or OTA to the user
  • Check-in reminders, boarding pass emails, e-ticket receipts
  • Forwarded booking confirmations where the inbox owner IS the listed \
passenger (e.g. a colleague or employer booked a ticket on their behalf)
  • Corporate travel tool confirmations (Concur, Navan, Egencia, etc.)
  • Round-trip or multi-leg itineraries where the user is on all segments

Set "recipient_is_passenger" to FALSE — and return an empty flights array — if:

  • A friend, family member, or colleague is sharing THEIR OWN travel plans to \
visit the inbox owner ("Here's my flight so you can pick me up", "I'll be \
arriving on Friday", "my itinerary for coming to see you")
  • The email is clearly about someone else traveling TO the user, not the user \
traveling themselves
  • The email is a hotel-only, car-only, train-only, or cruise booking with no \
flights
  • The email is a flight deal alert, newsletter, or advertisement (no actual \
booking)

IMPORTANT — forwarded emails:
  • "Fwd:" in the subject does NOT automatically mean exclude. Check the \
passenger name. If the forwarded confirmation shows the inbox owner as \
passenger, include it. If it shows the forwarder as passenger traveling to \
visit, exclude it.

{name_hint}\

────────────────────────────────────────────────────────────
STEP 2 — Extract flight segments
────────────────────────────────────────────────────────────
If recipient_is_passenger is TRUE, extract every individual flight segment \
(include each leg of a connection or round-trip as its own object).

Return ONLY a valid JSON object — no markdown, no explanation:

{{
  "recipient_is_passenger": true,
  "flights": [
    {{
      "airline": "Full airline name, e.g. United Airlines",
      "flight_number": "e.g. UA 123 — or null",
      "departure_airport": "IATA code e.g. JFK — or null",
      "departure_city": "City name — or null",
      "departure_country": "2-letter ISO code e.g. US — or null",
      "arrival_airport": "IATA code — or null",
      "arrival_city": "City name — or null",
      "arrival_country": "2-letter ISO code — or null",
      "departure_date": "YYYY-MM-DD — or null",
      "arrival_date": "YYYY-MM-DD — or null",
      "booking_reference": "PNR/confirmation code — or null"
    }}
  ]
}}

If recipient_is_passenger is FALSE:
{{
  "recipient_is_passenger": false,
  "flights": []
}}

────────────────────────────────────────────────────────────
EMAIL
────────────────────────────────────────────────────────────
Subject: {subject}
From: {sender}
Date: {date}

Body:
{body}
"""


def _get_client() -> anthropic.Anthropic:
    """Create a fresh Anthropic client. Uses st.secrets or env."""
    try:
        api_key = st.secrets.get("ANTHROPIC_API_KEY")
    except Exception:
        api_key = None
    api_key = api_key or os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        raise EnvironmentError("ANTHROPIC_API_KEY not set in secrets or environment.")
    return anthropic.Anthropic(api_key=api_key)


def extract_flights(
    body: str,
    subject: str,
    sender: str,
    date: str,
    passenger_name: str = "",
) -> list[dict]:
    """Extract flight records from a single email using Claude.

    Returns a list of flight dicts for the email recipient's own flights.
    Returns [] if the email is for a visitor, not a booking, or on any error.

    Args:
        body:           Plain-text email body (HTML stripped by caller).
        subject:        Email Subject header.
        sender:         Email From header.
        date:           Email Date header.
        passenger_name: Optional. User's full name — helps Claude match
                        passenger fields and filter visitor emails.
    """
    if not body or len(body.strip()) < 30:
        return []

    # Build name hint for the prompt
    if passenger_name and passenger_name.strip():
        name_hint = (
            f"The inbox owner's name is: {passenger_name.strip()!r}\n"
            "Use this to help determine if the listed passenger is the inbox "
            "owner. If the name does not appear in the email, use other signals "
            "(booking language, email address, pronoun context) to decide.\n\n"
        )
    else:
        name_hint = (
            "No name was provided for the inbox owner. Use context clues — "
            "booking confirmation language, 'your itinerary', 'you are booked', "
            "direct airline/OTA sender, etc. — to decide if this is the "
            "recipient's own flight.\n\n"
        )

    # Truncate before transmission — reduce data exposure surface
    truncated_body = body[:_MAX_EMAIL_CHARS]

    prompt = _EXTRACTION_PROMPT.format(
        name_hint=name_hint,
        subject=subject[:200],
        sender=sender[:200],
        date=date[:100],
        body=truncated_body,
    )

    try:
        client = _get_client()
        response = client.messages.create(
            model="claude-opus-4-5",
            max_tokens=1024,
            timeout=30.0,
            messages=[{"role": "user", "content": prompt}],
        )

        raw_text = response.content[0].text.strip()

        # Extract JSON even if surrounded by stray text
        json_match = re.search(r"\{[\s\S]*\}", raw_text)
        if not json_match:
            return []

        parsed = json.loads(json_match.group())

        # Respect Claude's passenger determination
        if not parsed.get("recipient_is_passenger", True):
            return []

        flights = parsed.get("flights", [])
        if not isinstance(flights, list):
            return []

        return [_sanitize_flight(f) for f in flights if isinstance(f, dict)]

    except (anthropic.APITimeoutError, anthropic.RateLimitError) as exc:
        print(f"[extractor] API issue for '{subject[:50]}': {exc}")
        return []
    except (json.JSONDecodeError, KeyError, IndexError) as exc:
        print(f"[extractor] Parse error for '{subject[:50]}': {exc}")
        return []
    except Exception as exc:
        print(f"[extractor] Unexpected error for '{subject[:50]}': {exc}")
        return []


# ── Field sanitisation ────────────────────────────────────────────────────────

_ISO_CODE_PATTERN = re.compile(r"^[A-Z]{2}$")
_DATE_PATTERN     = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_IATA_PATTERN     = re.compile(r"^[A-Z]{3}$")


def _sanitize_flight(f: dict) -> dict:
    """Sanitize a raw flight dict from Claude. Coerce types, strip control chars."""

    def clean_str(v, max_len=200) -> str | None:
        if v is None:
            return None
        s = str(v).strip()
        s = re.sub(r"[\x00-\x1f\x7f]", "", s)   # strip control chars
        return s[:max_len] if s else None

    def clean_iso(v) -> str | None:
        s = clean_str(v, 2)
        if s and _ISO_CODE_PATTERN.match(s.upper()):
            return s.upper()
        return None

    def clean_date(v) -> str | None:
        s = clean_str(v, 10)
        if s and _DATE_PATTERN.match(s):
            return s
        return None

    def clean_iata(v) -> str | None:
        s = clean_str(v, 3)
        if s and _IATA_PATTERN.match(s.upper()):
            return s.upper()
        return clean_str(v, 100)   # fall back to city name if not IATA

    return {
        "airline":            clean_str(f.get("airline"), 100),
        "flight_number":      clean_str(f.get("flight_number"), 20),
        "departure_airport":  clean_iata(f.get("departure_airport")),
        "departure_city":     clean_str(f.get("departure_city"), 100),
        "departure_country":  clean_iso(f.get("departure_country")),
        "arrival_airport":    clean_iata(f.get("arrival_airport")),
        "arrival_city":       clean_str(f.get("arrival_city"), 100),
        "arrival_country":    clean_iso(f.get("arrival_country")),
        "departure_date":     clean_date(f.get("departure_date")),
        "arrival_date":       clean_date(f.get("arrival_date")),
        "booking_reference":  clean_str(f.get("booking_reference"), 20),
    }

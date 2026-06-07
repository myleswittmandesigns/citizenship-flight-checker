"""
Claude AI flight data extractor.

Sends email text to Anthropic's API and returns structured flight records.
Email content is NOT stored by this application — it is sent to Anthropic
for processing. Users are informed of this in the UI before scanning.

Security notes:
  - Email body is truncated before transmission (reduce data exposure surface).
  - Claude response is parsed with strict JSON validation; malformed responses
    return [] rather than crashing.
  - Timeout enforced per API call (Fix M-2 analog).
"""

import json
import os
import re
import streamlit as st
import anthropic

# Max characters of email body sent to Claude — balances completeness vs exposure
_MAX_EMAIL_CHARS = 5_000

_EXTRACTION_PROMPT = """\
You are a precise flight data extractor. Extract all flight booking information \
from the email below.

Return ONLY a valid JSON object with a "flights" array. \
Each flight object must have exactly these fields:
- airline: string (full airline name, e.g. "United Airlines")
- flight_number: string (e.g. "UA 123") or null
- departure_airport: string (IATA code if available, e.g. "JFK") or null
- departure_city: string or null
- departure_country: string (2-letter ISO country code, e.g. "US") or null
- arrival_airport: string (IATA code if available) or null
- arrival_city: string or null
- arrival_country: string (2-letter ISO country code) or null
- departure_date: string (YYYY-MM-DD) or null
- arrival_date: string (YYYY-MM-DD) or null
- booking_reference: string (PNR/confirmation code) or null

Rules:
- Include ONLY actual flight segments (not hotels, cars, or train bookings)
- If multiple legs exist (round-trip or connections) include each as a separate object
- If the email is NOT a flight booking/confirmation, return {{"flights": []}}
- Return ONLY valid JSON — no explanation text, no markdown

Email metadata:
Subject: {subject}
From: {sender}
Date: {date}

Email content (may be truncated):
{body}
"""


def _get_client() -> anthropic.Anthropic:
    """Create a fresh Anthropic client. Uses st.secrets or env (Fix H-3 pattern)."""
    try:
        api_key = st.secrets.get("ANTHROPIC_API_KEY")
    except Exception:
        api_key = None
    api_key = api_key or os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        raise EnvironmentError("ANTHROPIC_API_KEY not set in secrets or environment.")
    return anthropic.Anthropic(api_key=api_key)


def extract_flights(email_text: str, subject: str, sender: str, date: str) -> list[dict]:
    """Extract flight records from a single email using Claude.

    Returns a list of flight dicts (may be empty). Never raises — on any
    error it logs and returns [].

    Args:
        email_text: Plain-text email body (HTML should be stripped by caller).
        subject:    Email subject header.
        sender:     Email From header.
        date:       Email Date header.
    """
    if not email_text or len(email_text.strip()) < 30:
        return []

    # Truncate before transmission — reduce data exposure surface
    truncated_body = email_text[:_MAX_EMAIL_CHARS]

    prompt = _EXTRACTION_PROMPT.format(
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
            timeout=30.0,   # Hard per-call timeout (Fix M-2 analog)
            messages=[{"role": "user", "content": prompt}],
        )

        raw_text = response.content[0].text.strip()

        # Extract JSON even if surrounded by explanation text
        json_match = re.search(r"\{[\s\S]*\}", raw_text)
        if not json_match:
            return []

        parsed = json.loads(json_match.group())
        flights = parsed.get("flights", [])

        if not isinstance(flights, list):
            return []

        return [_sanitize_flight(f) for f in flights if isinstance(f, dict)]

    except (anthropic.APITimeoutError, anthropic.RateLimitError) as exc:
        # Surface rate/timeout issues as a warning but don't crash
        print(f"[extractor] API issue for '{subject[:50]}': {exc}")
        return []
    except (json.JSONDecodeError, KeyError, IndexError) as exc:
        print(f"[extractor] Parse error for '{subject[:50]}': {exc}")
        return []
    except Exception as exc:
        print(f"[extractor] Unexpected error for '{subject[:50]}': {exc}")
        return []


_ISO_CODE_PATTERN = re.compile(r"^[A-Z]{2}$")
_DATE_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_IATA_PATTERN = re.compile(r"^[A-Z]{3}$")


def _sanitize_flight(f: dict) -> dict:
    """Sanitize a raw flight dict from Claude. Coerce types, strip control chars.

    This prevents Claude-sourced data from causing downstream issues.
    """

    def clean_str(v, max_len=200) -> str | None:
        if v is None:
            return None
        s = str(v).strip()
        # Remove control characters (potential injection vectors)
        s = re.sub(r"[\x00-\x1f\x7f]", "", s)
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
        return clean_str(v, 10)  # Fall back to city name if not IATA

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

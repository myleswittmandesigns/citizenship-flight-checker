"""
Gmail email scanner — fetches flight-related emails and coordinates extraction.

Security notes:
  - Uses per-call Credentials object (Fix C-1 — no shared OAuth singleton).
  - countryIsoCode validated against allowlist before use (Fix H-2).
  - lookbackYears bounded integer (Fix H-1).
  - Email content is never stored or logged — only passed to extractor.py.
"""

import base64
import re
from datetime import datetime, timedelta
from typing import Generator

from googleapiclient.discovery import build

from utils.extractor import extract_flights


def _strip_html(html: str) -> str:
    """Remove HTML tags and decode common entities from email body."""
    # Remove <style> and <script> blocks
    html = re.sub(r"<style[^>]*>[\s\S]*?</style>", " ", html, flags=re.IGNORECASE)
    html = re.sub(r"<script[^>]*>[\s\S]*?</script>", " ", html, flags=re.IGNORECASE)
    # Remove all other tags
    html = re.sub(r"<[^>]+>", " ", html)
    # Decode common HTML entities
    replacements = {
        "&nbsp;": " ", "&amp;": "&", "&lt;": "<",
        "&gt;": ">", "&quot;": '"', "&#39;": "'",
    }
    for entity, char in replacements.items():
        html = html.replace(entity, char)
    # Collapse whitespace
    return re.sub(r"\s{2,}", " ", html).strip()


def _decode_body(payload: dict) -> str:
    """Recursively extract and decode the best text body from a Gmail message payload."""
    plain_parts, html_parts = [], []

    def walk(part: dict):
        mime = part.get("mimeType", "")
        body = part.get("body", {})
        data = body.get("data")

        if data:
            decoded = base64.urlsafe_b64decode(data + "==").decode("utf-8", errors="replace")
            if mime == "text/plain":
                plain_parts.append(decoded)
            elif mime == "text/html":
                html_parts.append(_strip_html(decoded))

        for sub in part.get("parts", []):
            walk(sub)

    walk(payload)

    # Prefer plain text — avoids HTML stripping artifacts
    if plain_parts:
        return " ".join(plain_parts)
    return " ".join(html_parts)


def _build_search_queries(after_date_str: str) -> list[str]:
    """Build Gmail search queries to find flight-related emails."""
    after = f"after:{after_date_str}"
    return [
        # Subject keywords
        (
            f'subject:("flight confirmation" OR "booking confirmation" OR '
            f'"e-ticket" OR "itinerary" OR "boarding pass" OR '
            f'"travel confirmation" OR "your trip") {after}'
        ),
        # Major airlines and booking platforms
        (
            f"from:(united.com OR delta.com OR aa.com OR southwest.com OR "
            f"jetblue.com OR spirit.com OR alaskaair.com OR frontier.com OR "
            f"ryanair.com OR easyjet.com OR klm.com OR lufthansa.com OR "
            f"britishairways.com OR airfrance.com OR emirates.com OR "
            f"qatarairways.com OR singaporeair.com OR cathaypacific.com OR "
            f"aircanada.com OR qantas.com OR virginatlantic.com OR "
            f"iberia.com OR turkishairlines.com) {after}"
        ),
        # Online travel agencies
        (
            f"from:(expedia.com OR kayak.com OR orbitz.com OR travelocity.com OR "
            f"booking.com OR priceline.com OR tripadvisor.com OR "
            f"cheapflights.com OR hopper.com OR google.com) {after}"
        ),
    ]


def scan_emails(
    credentials,
    country_iso_code: str,
    lookback_years: int,
    progress_callback=None,
) -> list[dict]:
    """Scan Gmail for flight emails and return flights involving the given country.

    Args:
        credentials:      Google OAuth2 Credentials object (fresh per call, Fix C-1).
        country_iso_code: 2-letter ISO code — caller must validate against allowlist
                          before calling this function (Fix H-2).
        lookback_years:   Integer 1–15 — caller must validate (Fix H-1).
        progress_callback: Optional callable(processed, total, message) for UI updates.

    Returns:
        List of deduplicated, sanitized flight dicts involving country_iso_code.
    """
    # Validate inputs defensively (belt-and-suspenders — caller should also validate)
    country_iso_code = str(country_iso_code).strip().upper()
    if not re.match(r"^[A-Z]{2}$", country_iso_code):
        raise ValueError(f"Invalid ISO code: {country_iso_code!r}")
    lookback_years = int(lookback_years)
    if not (1 <= lookback_years <= 15):
        raise ValueError(f"lookback_years must be 1–15, got {lookback_years}")

    # Build date boundary
    after_date = datetime.now() - timedelta(days=lookback_years * 366)
    after_str = after_date.strftime("%Y/%m/%d")

    service = build("gmail", "v1", credentials=credentials, cache_discovery=False)

    # ── 1. Collect unique message IDs across all search queries ──────────────
    all_ids: set[str] = set()
    for query in _build_search_queries(after_str):
        page_token = None
        while True:
            params = {"userId": "me", "q": query, "maxResults": 500}
            if page_token:
                params["pageToken"] = page_token
            result = service.users().messages().list(**params).execute()
            for msg in result.get("messages", []):
                all_ids.add(msg["id"])
            page_token = result.get("nextPageToken")
            if not page_token:
                break

    message_ids = list(all_ids)
    total = len(message_ids)

    if progress_callback:
        progress_callback(0, total, f"Found {total} potential flight emails. Starting analysis…")

    if total == 0:
        return []

    # ── 2. Fetch and extract flights ─────────────────────────────────────────
    all_flights: list[dict] = []
    BATCH = 5  # Moderate concurrency — avoids Gmail API quota exhaustion

    for i in range(0, total, BATCH):
        batch_ids = message_ids[i : i + BATCH]

        for msg_id in batch_ids:
            try:
                msg = (
                    service.users()
                    .messages()
                    .get(userId="me", id=msg_id, format="full")
                    .execute()
                )
            except Exception:
                continue  # Skip unreachable messages — don't surface noise

            payload = msg.get("payload", {})
            headers = {h["name"].lower(): h["value"] for h in payload.get("headers", [])}

            subject = headers.get("subject", "")
            sender = headers.get("from", "")
            date = headers.get("date", "")

            body = _decode_body(payload)
            if not body:
                continue

            flights = extract_flights(body, subject, sender, date)
            all_flights.extend(flights)

        processed = min(i + BATCH, total)
        if progress_callback:
            progress_callback(
                processed,
                total,
                f"Analyzed {processed}/{total} emails — {len(all_flights)} flights found so far…",
            )

    # ── 3. Filter to flights involving the selected country ──────────────────
    relevant = [
        f for f in all_flights
        if f.get("departure_country") == country_iso_code
        or f.get("arrival_country") == country_iso_code
    ]

    # ── 4. Deduplicate by (flight_number OR airline, departure_date, airports) ─
    seen: set[str] = set()
    unique: list[dict] = []
    for f in relevant:
        key = "|".join([
            f.get("flight_number") or f.get("airline") or "",
            f.get("departure_date") or "",
            f.get("departure_airport") or f.get("departure_city") or "",
            f.get("arrival_airport") or f.get("arrival_city") or "",
        ])
        if key not in seen:
            seen.add(key)
            unique.append(f)

    # ── 5. Sort chronologically ───────────────────────────────────────────────
    unique.sort(key=lambda f: f.get("departure_date") or "9999-12-31")

    return unique

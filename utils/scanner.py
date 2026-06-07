"""
Gmail API email scanner — searches for flight-related emails via Google's API.

Advantages over IMAP:
  - No app passwords or 2-Step Verification setup required for users
  - Faster, server-side search (Gmail handles the query)
  - Handles Gmail-specific folders (Promotions, etc.) transparently
  - Pagination built into the API

Security: credentials passed per-call; no shared state (Fix C-1).
"""

import base64
import re
from datetime import datetime, timedelta

from googleapiclient.discovery import build

from utils.extractor import extract_flights

# ── Search queries ────────────────────────────────────────────────────────────
def _build_queries(after_str: str) -> list[str]:
    """Return Gmail search queries that cast a wide net for flight emails."""
    return [
        # Subject keywords
        (
            'subject:("flight confirmation" OR "booking confirmation" OR '
            '"e-ticket" OR "itinerary" OR "boarding pass" OR '
            '"travel confirmation" OR "trip confirmation" OR "electronic ticket") '
            f"after:{after_str}"
        ),
        # Major airline domains
        (
            "from:(united.com OR delta.com OR aa.com OR southwest.com OR "
            "jetblue.com OR spirit.com OR alaskaair.com OR frontier.com OR "
            "ryanair.com OR easyjet.com OR klm.com OR lufthansa.com OR "
            "britishairways.com OR airfrance.com OR emirates.com OR "
            "qatarairways.com OR singaporeair.com OR cathaypacific.com OR "
            "aircanada.com OR qantas.com OR virginatlantic.com OR "
            f"iberia.com OR turkishairlines.com) after:{after_str}"
        ),
        # Online travel agencies
        (
            "from:(expedia.com OR kayak.com OR orbitz.com OR travelocity.com OR "
            "priceline.com OR booking.com OR tripadvisor.com OR "
            f"hopper.com OR skyscanner.com OR cheapflights.com) after:{after_str}"
        ),
    ]


def _strip_html(html: str) -> str:
    html = re.sub(r"<style[^>]*>[\s\S]*?</style>", " ", html, flags=re.IGNORECASE)
    html = re.sub(r"<script[^>]*>[\s\S]*?</script>", " ", html, flags=re.IGNORECASE)
    html = re.sub(r"<[^>]+>", " ", html)
    for entity, char in [("&nbsp;", " "), ("&amp;", "&"), ("&lt;", "<"),
                          ("&gt;", ">"), ("&quot;", '"'), ("&#39;", "'")]:
        html = html.replace(entity, char)
    return re.sub(r"\s{2,}", " ", html).strip()


def _decode_body(payload: dict) -> str:
    """Recursively extract best-available text from a Gmail message payload."""
    plain, html = [], []

    def walk(part: dict):
        mime = part.get("mimeType", "")
        data = (part.get("body") or {}).get("data")
        if data:
            decoded = base64.urlsafe_b64decode(data + "==").decode("utf-8", errors="replace")
            if mime == "text/plain":
                plain.append(decoded)
            elif mime == "text/html":
                html.append(_strip_html(decoded))
        for sub in part.get("parts") or []:
            walk(sub)

    walk(payload)
    return " ".join(plain) if plain else " ".join(html)


def scan_emails(
    credentials,
    country_iso_code: str,
    lookback_years: int,
    progress_callback=None,
) -> list[dict]:
    """Scan Gmail for flight emails and return flights involving the given country.

    Args:
        credentials:      Fresh Google Credentials object (Fix C-1).
        country_iso_code: 2-letter ISO code — pre-validated by caller (Fix H-2).
        lookback_years:   Integer 1–15 — pre-validated by caller (Fix H-1).
        progress_callback: Optional callable(processed, total, message).

    Returns:
        Deduplicated, chronologically sorted list of relevant flight dicts.
    """
    after_dt  = datetime.now() - timedelta(days=lookback_years * 366)
    after_str = after_dt.strftime("%Y/%m/%d")

    service = build("gmail", "v1", credentials=credentials, cache_discovery=False)

    # ── Collect unique message IDs across all queries ─────────────────────────
    all_ids: set[str] = set()
    for query in _build_queries(after_str):
        page_token = None
        while True:
            params = {"userId": "me", "q": query, "maxResults": 500}
            if page_token:
                params["pageToken"] = page_token
            result = service.users().messages().list(**params).execute()
            for msg in result.get("messages") or []:
                all_ids.add(msg["id"])
            page_token = result.get("nextPageToken")
            if not page_token:
                break

    message_ids = list(all_ids)
    total = len(message_ids)

    if progress_callback:
        progress_callback(0, total, f"Found {total} potential flight emails. Analyzing with AI…")

    if total == 0:
        return []

    # ── Fetch and extract ─────────────────────────────────────────────────────
    all_flights: list[dict] = []
    BATCH = 5

    for i, msg_id in enumerate(message_ids):
        try:
            msg     = service.users().messages().get(userId="me", id=msg_id, format="full").execute()
            payload = msg.get("payload", {})
            headers = {h["name"].lower(): h["value"] for h in payload.get("headers") or []}

            subject = headers.get("subject", "")
            sender  = headers.get("from", "")
            date    = headers.get("date", "")
            body    = _decode_body(payload)

            if body:
                all_flights.extend(extract_flights(body, subject, sender, date))
        except Exception:
            pass

        if progress_callback and (i + 1) % BATCH == 0:
            progress_callback(
                i + 1, total,
                f"Analyzed {i + 1}/{total} emails — {len(all_flights)} flights found so far…",
            )

    if progress_callback:
        progress_callback(total, total, "Analysis complete.")

    # ── Filter to flights involving the selected country ──────────────────────
    relevant = [
        f for f in all_flights
        if f.get("departure_country") == country_iso_code
        or f.get("arrival_country")   == country_iso_code
    ]

    # ── Deduplicate ───────────────────────────────────────────────────────────
    seen: set[str] = set()
    unique: list[dict] = []
    for f in relevant:
        key = "|".join([
            f.get("flight_number") or f.get("airline") or "",
            f.get("departure_date") or "",
            f.get("departure_airport") or f.get("departure_city") or "",
            f.get("arrival_airport")   or f.get("arrival_city")   or "",
        ])
        if key not in seen:
            seen.add(key)
            unique.append(f)

    unique.sort(key=lambda f: f.get("departure_date") or "9999-12-31")
    return unique

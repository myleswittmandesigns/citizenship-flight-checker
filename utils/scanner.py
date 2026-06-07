"""
IMAP email scanner — searches for flight-related emails and coordinates extraction.

Works with any IMAP provider (Gmail, Outlook, Yahoo, iCloud, custom).
Credentials are passed in per-call; no shared state between users.
"""

import email
import imaplib
from datetime import datetime, timedelta
from email.header import decode_header as _decode_header

from utils.extractor import extract_flights

# ── Search terms ──────────────────────────────────────────────────────────────
# IMAP SEARCH SUBJECT queries — flight-related email subjects
_SUBJECT_TERMS = [
    "flight confirmation",
    "booking confirmation",
    "e-ticket",
    "itinerary",
    "boarding pass",
    "travel confirmation",
    "trip confirmation",
    "flight receipt",
    "your booking",
    "your trip",
    "electronic ticket",
    "ticket receipt",
    "reservation confirmation",
]

# Common airline / travel agency sender domains — searched via FROM
_SENDER_DOMAINS = [
    "united.com", "delta.com", "aa.com", "southwest.com",
    "jetblue.com", "spirit.com", "alaskaair.com", "frontier.com",
    "ryanair.com", "easyjet.com", "klm.com", "lufthansa.com",
    "britishairways.com", "airfrance.com", "emirates.com",
    "qatarairways.com", "singaporeair.com", "cathaypacific.com",
    "aircanada.com", "qantas.com", "virginatlantic.com",
    "iberia.com", "turkishairlines.com", "flydubai.com",
    "expedia.com", "kayak.com", "orbitz.com", "travelocity.com",
    "priceline.com", "booking.com", "tripadvisor.com",
    "cheapflights.com", "hopper.com", "skyscanner.com",
]


def _decode_str(value: str | bytes | None) -> str:
    """Safely decode a potentially encoded email header value."""
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    parts = _decode_header(value)
    result = []
    for part, charset in parts:
        if isinstance(part, bytes):
            result.append(part.decode(charset or "utf-8", errors="replace"))
        else:
            result.append(str(part))
    return " ".join(result)


def _get_body(msg: email.message.Message) -> str:
    """Extract plain-text body from a MIME message, with HTML fallback."""
    plain, html = [], []

    def walk(part: email.message.Message):
        ctype = part.get_content_type()
        disposition = str(part.get("Content-Disposition", ""))
        if "attachment" in disposition:
            return
        payload = part.get_payload(decode=True)
        if payload is None:
            return
        charset = part.get_content_charset() or "utf-8"
        text = payload.decode(charset, errors="replace")
        if ctype == "text/plain":
            plain.append(text)
        elif ctype == "text/html":
            html.append(_strip_html(text))
        # Recurse into multipart
        if part.is_multipart():
            for sub in part.get_payload():
                walk(sub)

    if msg.is_multipart():
        for part in msg.get_payload():
            walk(part)
    else:
        walk(msg)

    return " ".join(plain) if plain else " ".join(html)


def _strip_html(html: str) -> str:
    """Remove HTML tags and decode common entities."""
    import re
    html = re.sub(r"<style[^>]*>[\s\S]*?</style>", " ", html, flags=re.IGNORECASE)
    html = re.sub(r"<script[^>]*>[\s\S]*?</script>", " ", html, flags=re.IGNORECASE)
    html = re.sub(r"<[^>]+>", " ", html)
    for entity, char in [
        ("&nbsp;", " "), ("&amp;", "&"), ("&lt;", "<"),
        ("&gt;", ">"), ("&quot;", '"'), ("&#39;", "'"),
    ]:
        html = html.replace(entity, char)
    import re as _re
    return _re.sub(r"\s{2,}", " ", html).strip()


def _imap_date(dt: datetime) -> str:
    """Format a datetime as IMAP SEARCH date: DD-Mon-YYYY."""
    return dt.strftime("%d-%b-%Y")


def _search_folder(
    imap: imaplib.IMAP4_SSL,
    folder: str,
    since_str: str,
) -> set[bytes]:
    """Search one folder for all flight-related message IDs."""
    try:
        status, _ = imap.select(f'"{folder}"', readonly=True)
        if status != "OK":
            return set()
    except Exception:
        return set()

    found: set[bytes] = set()

    # Search by subject keywords
    for term in _SUBJECT_TERMS:
        try:
            _, data = imap.search(None, f'SINCE {since_str} SUBJECT "{term}"')
            if data and data[0]:
                found.update(data[0].split())
        except Exception:
            continue

    # Search by sender domains
    for domain in _SENDER_DOMAINS:
        try:
            _, data = imap.search(None, f'SINCE {since_str} FROM "{domain}"')
            if data and data[0]:
                found.update(data[0].split())
        except Exception:
            continue

    return found


def scan_emails(
    creds: dict,
    country_iso_code: str,
    lookback_years: int,
    progress_callback=None,
) -> list[dict]:
    """Connect via IMAP, search for flight emails, extract and return flights.

    Args:
        creds:            Dict from auth.get_credentials() — email, password, host, port, folders.
        country_iso_code: 2-letter ISO code (pre-validated by caller).
        lookback_years:   Integer 1–15 (pre-validated by caller).
        progress_callback: Optional callable(processed, total, message).

    Returns:
        Deduplicated, sorted list of flight dicts involving country_iso_code.
    """
    from utils import auth as _auth

    since_dt = datetime.now() - timedelta(days=lookback_years * 366)
    since_str = _imap_date(since_dt)

    imap = _auth.connect(
        host=creds["host"],
        port=creds["port"],
        email_address=creds["email"],
        app_password=creds["password"],
    )

    try:
        # ── Collect unique message IDs across all folders ─────────────────────
        all_ids: set[bytes] = set()
        for folder in creds.get("folders", ["INBOX"]):
            folder_ids = _search_folder(imap, folder, since_str)
            all_ids.update(folder_ids)

        message_ids = list(all_ids)
        total = len(message_ids)

        if progress_callback:
            progress_callback(0, total, f"Found {total} potential flight emails. Analyzing…")

        if total == 0:
            return []

        # ── Fetch and extract flights ─────────────────────────────────────────
        all_flights: list[dict] = []
        BATCH = 5

        # Re-select INBOX for fetching (most IDs will be from INBOX)
        imap.select('"INBOX"', readonly=True)

        for i, msg_id in enumerate(message_ids):
            try:
                # Fetch full RFC822 message
                _, msg_data = imap.fetch(msg_id, "(RFC822)")
                if not msg_data or msg_data[0] is None:
                    continue

                raw = msg_data[0][1]
                if isinstance(raw, str):
                    raw = raw.encode("utf-8")

                msg = email.message_from_bytes(raw)

                subject = _decode_str(msg.get("Subject", ""))
                sender  = _decode_str(msg.get("From", ""))
                date    = _decode_str(msg.get("Date", ""))
                body    = _get_body(msg)

                if not body:
                    continue

                flights = extract_flights(body, subject, sender, date)
                all_flights.extend(flights)

            except Exception:
                pass  # Skip individual message failures silently

            if progress_callback and (i + 1) % BATCH == 0:
                progress_callback(
                    i + 1, total,
                    f"Analyzed {i + 1}/{total} emails — {len(all_flights)} flights found so far…",
                )

        if progress_callback:
            progress_callback(total, total, f"Analysis complete — {len(all_flights)} total flights found.")

    finally:
        try:
            imap.logout()
        except Exception:
            pass

    # ── Filter to flights involving the selected country ─────────────────────
    relevant = [
        f for f in all_flights
        if f.get("departure_country") == country_iso_code
        or f.get("arrival_country") == country_iso_code
    ]

    # ── Deduplicate ───────────────────────────────────────────────────────────
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

    # ── Sort chronologically ──────────────────────────────────────────────────
    unique.sort(key=lambda f: f.get("departure_date") or "9999-12-31")
    return unique

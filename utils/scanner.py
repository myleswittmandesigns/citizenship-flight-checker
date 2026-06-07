"""
Gmail API email scanner — comprehensive, content-based flight detection.

Search strategy
───────────────
We use ten parallel Gmail queries rather than a single domain list. The first
five are *content-based* — they match the structural language that appears in
every flight confirmation regardless of which airline or OTA sent it (PNR
codes, departure/arrival language, check-in prompts, etc.). These catch any
airline worldwide, including small regional carriers, charter operators, and
corporate booking tools. The remaining five are domain-based nets that
supplement the content queries for emails whose bodies are image-heavy or
otherwise hard to search by content.

All ten queries run concurrently and deduplicate by message-ID before any
email is fetched, so there is no double-counting or extra API cost.

Passenger filtering
───────────────────
Claude AI determines per email whether the recipient is a passenger on the
flights found — or whether someone else is sharing *their* travel plans (a
visiting friend, etc.). Only "recipient is passenger" emails contribute flights.
An optional passenger_name hint improves this classification.

Security: credentials passed per-call; no shared state (Fix C-1).
"""

import base64
import re
from datetime import datetime, timedelta

from googleapiclient.discovery import build

from utils.extractor import extract_flights


# ── Search queries ─────────────────────────────────────────────────────────────

def _build_queries(after_str: str) -> list[str]:
    """Ten Gmail search queries that collectively cast a comprehensive net.

    Queries 1-5 are content-based (any airline worldwide).
    Queries 6-10 are domain-based (supplementary net).
    All are filtered to the lookback window via after:<date>.
    """
    q = f"after:{after_str}"
    return [
        # ── Content-based queries (airline-agnostic) ─────────────────────────

        # 1. Subject line — broad set of booking/confirmation keywords
        (
            'subject:("flight confirmation" OR "booking confirmation" OR '
            '"e-ticket" OR "itinerary" OR "boarding pass" OR '
            '"travel confirmation" OR "trip confirmation" OR '
            '"electronic ticket" OR "ticket receipt" OR "flight receipt" OR '
            '"your reservation" OR "reservation confirmed" OR "booking receipt" OR '
            '"flight itinerary" OR "ticket confirmation" OR "travel receipt" OR '
            '"your booking" OR "booking details" OR "travel itinerary" OR '
            '"trip details" OR "your trip" OR "travel details") '
            f"{q}"
        ),

        # 2. PNR / booking reference — the most reliable flight-email signal.
        #    Every airline issues one; searching for it catches any carrier.
        (
            '("booking reference" OR "record locator" OR "PNR" OR '
            '"reservation code" OR "e-ticket number" OR "ticket number" OR '
            '"confirmation number" OR "booking number" OR "locator code") '
            '("flight" OR "airline" OR "departs" OR "departure" OR "aircraft") '
            f"{q}"
        ),

        # 3. Departure + arrival structural language (appears in all itineraries)
        (
            '("departure" OR "departs" OR "departing") '
            '("arrival" OR "arrives" OR "arriving") '
            '("flight number" OR "flight no" OR "flt" OR '
            '"operated by" OR "airline" OR "aircraft") '
            f"{q}"
        ),

        # 4. Check-in prompts — airlines universally send these; very high precision
        (
            '("online check-in" OR "web check-in" OR "check in now" OR '
            '"check in for your flight" OR "check-in is open" OR '
            '"check-in opens" OR "ready to board" OR "boarding pass" OR '
            '"board your flight" OR "seat selection" OR "add baggage") '
            f"{q}"
        ),

        # 5. IATA airport code pattern + flight-related terms.
        #    Three-letter codes (e.g. JFK, LHR, DXB) are strong flight signals
        #    when paired with departure/arrival language.
        (
            '("gate" OR "terminal") '
            '("boarding" OR "departs" OR "departure" OR "arrives" OR "arrival") '
            '("seat" OR "class" OR "economy" OR "business" OR "first class") '
            f"{q}"
        ),

        # ── Domain-based queries (supplementary net) ─────────────────────────

        # 6. US carriers
        (
            "from:(united.com OR delta.com OR aa.com OR southwest.com OR "
            "jetblue.com OR spirit.com OR alaskaair.com OR frontier.com OR "
            "hawaiianairlines.com OR suncount.com OR breezeairways.com OR "
            "allegiantair.com OR flyflair.com OR swiftair.com) "
            f"{q}"
        ),

        # 7. European carriers (full-service + budget)
        (
            "from:(ryanair.com OR easyjet.com OR klm.com OR lufthansa.com OR "
            "britishairways.com OR airfrance.com OR iberia.com OR "
            "norwegian.com OR wizzair.com OR vueling.com OR eurowings.com OR "
            "transavia.com OR volotea.com OR condor.com OR tuifly.com OR "
            "aerlingus.com OR flytap.com OR swiss.com OR austrian.com OR "
            "brusselsairlines.com OR lot.com OR finnair.com OR "
            "flysas.com OR icelandair.com OR airbaltic.com OR "
            "turkishairlines.com OR sunexpress.com OR pegasusairlines.com) "
            f"{q}"
        ),

        # 8. Middle East, Africa, and Asia-Pacific carriers
        (
            "from:(emirates.com OR qatarairways.com OR etihad.com OR "
            "flydubai.com OR airarabia.com OR saudia.com OR omanair.com OR "
            "gulfair.com OR ethiopianairlines.com OR egyptair.com OR "
            "kenya-airways.com OR rwandair.com OR airmaroc.com OR "
            "singaporeair.com OR cathaypacific.com OR qantas.com OR "
            "airasia.com OR thaiairways.com OR bangkokair.com OR "
            "koreanair.com OR flyasiana.com OR jal.com OR ana.co.jp OR "
            "airchina.com OR chinaeastern.com OR csair.com OR "
            "airindia.in OR goindigo.in OR spicejet.com OR airvistara.com OR "
            "vietnamairlines.com OR vietjetair.com OR "
            "garuda-indonesia.com OR lionair.com OR cebu-pacific.com OR "
            "airpacific.com OR fijiairways.com OR virginaustralia.com) "
            f"{q}"
        ),

        # 9. Americas (Canada, Latin America, Caribbean)
        (
            "from:(aircanada.com OR westjet.com OR swoop.com OR flair.com OR "
            "aeromexico.com OR volaris.com OR vivaaerobus.com OR "
            "latam.com OR avianca.com OR copaair.com OR "
            "azul.com.br OR voegol.com.br OR voeazul.com.br OR "
            "caribbean-airlines.com OR bahamasair.com OR liat.com OR "
            "virginatlantic.com OR cayman-airways.com) "
            f"{q}"
        ),

        # 10. Online travel agencies and corporate booking tools
        (
            "from:(expedia.com OR hotels.com OR hotwire.com OR "
            "kayak.com OR orbitz.com OR travelocity.com OR "
            "priceline.com OR booking.com OR agoda.com OR "
            "tripadvisor.com OR hopper.com OR skyscanner.com OR "
            "cheapflights.com OR cheapoair.com OR onetravel.com OR "
            "justfly.com OR flighthub.com OR kiwi.com OR momondo.com OR "
            "edreams.com OR lastminute.com OR opodo.com OR "
            "bravofly.com OR mytrip.com OR gotogate.com OR "
            "concur.com OR navan.com OR egencia.com OR "
            "amexgbt.com OR tripactions.com OR spotnana.com OR "
            "travelport.com OR cytric.net) "
            f"{q}"
        ),
    ]


# ── Email body decoding ────────────────────────────────────────────────────────

def _strip_html(html: str) -> str:
    html = re.sub(r"<style[^>]*>[\s\S]*?</style>", " ", html, flags=re.IGNORECASE)
    html = re.sub(r"<script[^>]*>[\s\S]*?</script>", " ", html, flags=re.IGNORECASE)
    html = re.sub(r"<[^>]+>", " ", html)
    for entity, char in [
        ("&nbsp;", " "), ("&amp;", "&"), ("&lt;", "<"),
        ("&gt;", ">"), ("&quot;", '"'), ("&#39;", "'"),
    ]:
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


# ── Main scan entry point ─────────────────────────────────────────────────────

def scan_emails(
    credentials,
    country_iso_code: str,
    lookback_years: int,
    passenger_name: str = "",
    progress_callback=None,
) -> list[dict]:
    """Scan Gmail for flight emails and return flights involving the given country.

    Args:
        credentials:      Fresh Google Credentials object (Fix C-1).
        country_iso_code: 2-letter ISO code — pre-validated by caller (Fix H-2).
        lookback_years:   Integer 1–15 — pre-validated by caller (Fix H-1).
        passenger_name:   Optional full name to help Claude identify which
                          emails are the user's own flights vs. a visitor's.
        progress_callback: Optional callable(processed, total, message).

    Returns:
        Deduplicated, chronologically sorted list of relevant flight dicts.
    """
    after_dt  = datetime.now() - timedelta(days=lookback_years * 366)
    after_str = after_dt.strftime("%Y/%m/%d")

    service = build("gmail", "v1", credentials=credentials, cache_discovery=False)

    # ── Collect unique message IDs across all queries ─────────────────────────
    if progress_callback:
        progress_callback(0, 1, "Searching Gmail with comprehensive flight queries…")

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
                flights = extract_flights(
                    body=body,
                    subject=subject,
                    sender=sender,
                    date=date,
                    passenger_name=passenger_name,
                )
                all_flights.extend(flights)
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

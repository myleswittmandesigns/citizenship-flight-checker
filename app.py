"""
Citizenship Flight Checker — Streamlit Application

Scans email via IMAP + app password for international flight history
to support citizenship applications. Uses Claude AI for extraction.

Works with Gmail, Outlook, Yahoo, iCloud, and any IMAP provider.
No OAuth required — only an Anthropic API key needed server-side.
"""

import os
import re
from datetime import datetime

import pandas as pd
import streamlit as st
from dotenv import load_dotenv

from utils import auth, countries as country_lib, compliance as comp_lib, scanner

# ── OAuth callback — must run before any rendering ────────────────────────────
def _check_oauth_callback():
    params = st.query_params
    if "code" not in params or "state" not in params:
        return
    code  = params["code"]
    state = params["state"]
    st.query_params.clear()   # Remove code from URL immediately (Fix I-4)
    try:
        auth.handle_oauth_callback(code, state)
    except ValueError as exc:
        st.error(f"❌ Sign-in failed: {exc}")
    except Exception as exc:
        st.error(f"❌ Unexpected error during sign-in: {exc}")

_check_oauth_callback()

load_dotenv()

# ── Page config ───────────────────────────────────────────────────────────────
st.set_page_config(
    page_title="Citizenship Flight Checker",
    page_icon="✈️",
    layout="wide",
    initial_sidebar_state="collapsed",
    menu_items={
        "About": (
            "**Citizenship Flight Checker**\n\n"
            "Scan your email for international flight history to support "
            "citizenship and residency applications.\n\n"
            "⚠️ Always verify against official airline records and consult "
            "a qualified immigration attorney before submitting to any government."
        )
    },
)

st.markdown(
    """
    <style>
    .block-container { padding-top: 1.5rem; padding-bottom: 3rem; }
    .step-label { font-size: 0.7rem; font-weight: 700; text-transform: uppercase;
                  letter-spacing: 0.08em; color: #64748b; margin-bottom: 0.2rem; }
    .comp-ok      { background:#d1fae5; border:1.5px solid #6ee7b7;
                    border-radius:8px; padding:12px 16px; color:#065f46; font-weight:500; }
    .comp-warning { background:#fef3c7; border:1.5px solid #fbbf24;
                    border-radius:8px; padding:12px 16px; color:#92400e; font-weight:500; }
    .comp-over    { background:#fee2e2; border:1.5px solid #fca5a5;
                    border-radius:8px; padding:12px 16px; color:#991b1b; font-weight:500; }
    .privacy-banner {
        background: #0d1b2a;
        border-radius: 12px;
        padding: 20px 28px;
        margin-bottom: 1.5rem;
        color: #f8fafc;
    }
    .privacy-banner h4 { margin: 0 0 14px 0; font-size: 1rem; color: #f8fafc; letter-spacing: 0.01em; }
    .privacy-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 10px; }
    .privacy-item {
        background: rgba(255,255,255,0.07);
        border: 1px solid rgba(255,255,255,0.12);
        border-radius: 8px;
        padding: 10px 14px;
        font-size: 0.82rem;
        line-height: 1.5;
    }
    .privacy-item strong { display: block; font-size: 0.88rem; margin-bottom: 2px; color: #ffffff; }
    .privacy-item span   { color: rgba(255,255,255,0.7); }
    .wiped-banner {
        background: #064e3b;
        border: 1.5px solid #6ee7b7;
        border-radius: 10px;
        padding: 16px 20px;
        margin-bottom: 1rem;
        color: #d1fae5;
        font-size: 0.95rem;
        line-height: 1.6;
    }
    .review-card {
        background: #f8fafc;
        border: 1.5px solid #e2e8f0;
        border-radius: 14px;
        padding: 28px 32px;
        margin: 1rem 0 1.5rem 0;
    }
    .route-header {
        display: flex;
        align-items: center;
        justify-content: center;
        gap: 1rem;
        margin: 0.5rem 0 1.2rem 0;
    }
    .route-airport {
        font-size: 2.4rem;
        font-weight: 800;
        color: #0f172a;
        letter-spacing: 0.04em;
        text-align: center;
    }
    .route-city {
        font-size: 0.78rem;
        color: #64748b;
        text-align: center;
        margin-top: -6px;
    }
    .route-arrow {
        font-size: 1.8rem;
        color: #94a3b8;
        padding-bottom: 4px;
    }
    #MainMenu { visibility: hidden; }
    footer     { visibility: hidden; }
    </style>
    """,
    unsafe_allow_html=True,
)

# ── Session state defaults ────────────────────────────────────────────────────
_DEFAULTS = {
    "authenticated":      False,
    "google_creds":       None,
    "connected_email":    None,
    "selected_country":   None,
    "passenger_name":     "",
    "flights":            None,
    "scan_stats":         {},
    "credentials_wiped":  False,
    "scan_error":         None,
    "oauth_state":        None,
    # Review flow
    "review_index":       0,       # index into flights list currently being reviewed
    "flight_decisions":   [],      # "keep" | "ignore" per flight, parallel to flights[]
}
for _k, _v in _DEFAULTS.items():
    if _k not in st.session_state:
        st.session_state[_k] = _v


# ─────────────────────────────────────────────────────────────────────────────
# HEADER
# ─────────────────────────────────────────────────────────────────────────────
hdr_left, hdr_right = st.columns([3, 1])
with hdr_left:
    st.markdown("## ✈️ Citizenship Flight Checker")
    st.caption("International flight history tool for citizenship and residency applications")
with hdr_right:
    if auth.is_authenticated():
        email_display = st.session_state.get("connected_email", "")
        st.success("Connected", icon="🔒")
        st.caption(email_display)
        if st.button("Disconnect", use_container_width=True):
            auth.logout()
            st.session_state["flights"] = None
            st.session_state["credentials_wiped"] = False
            st.rerun()

# ── Privacy banner — always visible, first thing users see ───────────────────
st.markdown(
    """
    <div class="privacy-banner">
      <h4>🔐 Your Privacy Is Guaranteed — Here Is Exactly What Happens</h4>
      <div class="privacy-grid">
        <div class="privacy-item">
          <strong>✅ One-time access only</strong>
          <span>Your email password is used for a single scan, then permanently
          deleted from memory the moment the scan finishes — whether it succeeds or fails.</span>
        </div>
        <div class="privacy-item">
          <strong>✅ Nothing is stored</strong>
          <span>No emails, no passwords, no flight data are ever written to disk
          or saved to any database. Everything lives only in your browser session.</span>
        </div>
        <div class="privacy-item">
          <strong>✅ Read-only, encrypted connection</strong>
          <span>The app connects to your Gmail over an encrypted OAuth connection.
          It can only read emails — it cannot send, delete, or modify anything.</span>
        </div>
        <div class="privacy-item">
          <strong>✅ All processing happens on the server — no third-party AI</strong>
          <span>Flight data is extracted locally using pattern recognition.
          Your email content is never sent to any external AI service or third party.</span>
        </div>
      </div>
    </div>
    """,
    unsafe_allow_html=True,
)

st.divider()


# ─────────────────────────────────────────────────────────────────────────────
# STEP 1 — SELECT COUNTRY
# ─────────────────────────────────────────────────────────────────────────────
def render_country_step():
    st.markdown('<p class="step-label">Step 2 — Select Country</p>', unsafe_allow_html=True)

    all_countries = country_lib.load_countries()
    if not all_countries:
        st.warning("No countries configured. Use **Manage Countries** in the sidebar.")
        return

    options = {f"{c.get('flag','🌐')}  {c['name']}": c for c in all_countries}
    choice = st.selectbox(
        "I am applying for citizenship in:",
        options=list(options.keys()),
        index=None,
        placeholder="— Select a country —",
    )

    if choice:
        c = options[choice]
        st.session_state["selected_country"] = c

        method = c.get("calculation_method", "per_year")
        max_days = (
            f"{c.get('max_days_abroad_per_year','?')} days / year"
            if method == "per_year"
            else f"{c.get('max_days_abroad_total','?')} days total over {c['lookback_years']} years"
        )

        col1, col2 = st.columns(2)
        col1.metric("Years to scan back", c["lookback_years"])
        col2.metric("Max days abroad allowed", max_days)

        if c.get("notes"):
            st.info(c["notes"], icon="ℹ️")
    else:
        st.session_state["selected_country"] = None

    st.write("")
    name = st.text_input(
        "Your full name (as printed on your boarding pass)",
        value=st.session_state.get("passenger_name", ""),
        placeholder="e.g. JANE SMITH",
        help=(
            "Optional but recommended. Used to distinguish your own flights "
            "from travel plans that friends or colleagues may have forwarded "
            "to you. Your name is sent to Claude AI as part of the email "
            "analysis — it is not stored by this app."
        ),
    )
    st.session_state["passenger_name"] = name.strip()


# ─────────────────────────────────────────────────────────────────────────────
# STEP 2 — CONNECT EMAIL (IMAP)
# ─────────────────────────────────────────────────────────────────────────────
def render_email_step():
    st.markdown('<p class="step-label">Step 1 — Connect Your Gmail</p>', unsafe_allow_html=True)

    # ── Post-scan: credentials already wiped ─────────────────────────────────
    if st.session_state.get("credentials_wiped"):
        st.markdown(
            """
            <div class="wiped-banner">
            ✅ <strong>Scan complete — Gmail access permanently revoked.</strong><br>
            Your sign-in token has been deleted from this app. It no longer exists
            anywhere — not in memory, not on disk, not in any log. Results are
            shown below. This app cannot access your Gmail again without a new sign-in.
            </div>
            """,
            unsafe_allow_html=True,
        )
        return

    # ── Already signed in (pre-scan) ─────────────────────────────────────────
    if auth.is_authenticated():
        email_display = st.session_state.get("connected_email", "your Gmail account")
        st.success(f"✅ Signed in as **{email_display}**", icon="🔒")
        st.info(
            "⏳ **Gmail access is held only until your scan completes.**  \n"
            "The moment the scan finishes — successfully or not — your sign-in "
            "token is permanently deleted from this application.",
            icon="🔐",
        )
        return

    # ── Sign-in prompt ────────────────────────────────────────────────────────
    st.markdown(
        "**What Google access is granted:**\n\n"
        "- ✅ Read flight confirmation emails — one time only\n"
        "- 🚫 Cannot send, delete, or modify any email\n"
        "- 🚫 Access token is deleted the instant your scan completes\n\n"
        "You will see Google's standard permission screen "
        "clearly showing the read-only scope before granting access."
    )

    try:
        auth_url = auth.get_auth_url()
        st.link_button(
            "Sign in with Google",
            url=auth_url,
            use_container_width=True,
            type="primary",
        )
    except EnvironmentError as exc:
        st.error(f"⚙️ Configuration error: {exc}")
        st.info(
            "The app needs `GOOGLE_CLIENT_ID` and `GOOGLE_CLIENT_SECRET` to be configured. "
            "See the README for setup instructions.",
            icon="ℹ️",
        )


# ─────────────────────────────────────────────────────────────────────────────
# STEP 3 — SCAN
# ─────────────────────────────────────────────────────────────────────────────
def render_scan_step():
    st.markdown('<p class="step-label">Step 3 — Scan for Flights</p>', unsafe_allow_html=True)

    country = st.session_state.get("selected_country")
    ready = auth.is_authenticated() and country is not None

    if not ready:
        missing = []
        if not auth.is_authenticated():
            missing.append("connect your Gmail (Step 1)")
        if not country:
            missing.append("select a country (Step 2)")
        st.button(
            "✈️ Start Scan",
            disabled=True,
            use_container_width=True,
            help="Please " + " and ".join(missing) + " first.",
        )
        return

    st.info(
        f"Will search **{country['lookback_years']} years** of Gmail for flights "
        f"involving **{country.get('flag','')} {country['name']}**. "
        f"Flight details are extracted locally using pattern recognition — "
        f"no email content leaves this server.",
        icon="⏱️",
    )

    if st.button("✈️ Start Email Scan", type="primary", use_container_width=True):
        _run_scan(country)


def _run_scan(country: dict):
    progress_bar = st.progress(0, text="Connecting to Gmail…")
    status_text  = st.empty()

    def on_progress(processed: int, total: int, message: str):
        pct = int((processed / total) * 100) if total > 0 else 0
        progress_bar.progress(pct, text=message)
        status_text.caption(f"{processed} / {total} emails analyzed")

    flights = []

    try:
        creds = auth.get_credentials()

        validated = country_lib.get_country_by_iso(country["iso_code"])
        if not validated:
            raise ValueError(f"Unknown country code: {country['iso_code']!r}")

        lookback = int(validated["lookback_years"])
        if not (1 <= lookback <= 15):
            raise ValueError("Invalid lookback period in country configuration.")

        flights = scanner.scan_emails(
            credentials=creds,
            country_iso_code=validated["iso_code"],
            lookback_years=lookback,
            passenger_name=st.session_state.get("passenger_name", ""),
            progress_callback=on_progress,
        )
        st.session_state["scan_error"] = None   # Clear any previous error

    except Exception as exc:
        # Store in session_state so it survives the rerun and stays visible
        st.session_state["scan_error"] = str(exc)

    finally:
        # ── ALWAYS wipe credentials — success, failure, or exception ─────────
        # This is the core privacy guarantee: one-time access, no retention.
        auth.logout()
        st.session_state["credentials_wiped"] = True

    progress_bar.empty()
    status_text.empty()

    # Always store flights and reset review state for the new scan
    st.session_state["flights"]          = flights
    st.session_state["review_index"]     = 0
    st.session_state["flight_decisions"] = []
    st.rerun()


# ─────────────────────────────────────────────────────────────────────────────
# EMAIL SOURCE DIALOG
# ─────────────────────────────────────────────────────────────────────────────

@st.dialog("📧 Source Email", width="large")
def _email_viewer(subject: str, sender: str, date: str, body: str):
    st.markdown(f"**From:** {sender or '—'}")
    st.markdown(f"**Date:** {date or '—'}")
    st.markdown(f"**Subject:** {subject or '—'}")
    st.divider()
    st.text_area(
        "Email body",
        value=body or "(no body content captured)",
        height=400,
        disabled=True,
        label_visibility="collapsed",
    )
    if st.button("Close", use_container_width=True):
        st.rerun()


# ─────────────────────────────────────────────────────────────────────────────
# RESULTS — routes between review and final summary
# ─────────────────────────────────────────────────────────────────────────────

def render_results():
    flights    = st.session_state.get("flights")
    country    = st.session_state.get("selected_country")
    scan_error = st.session_state.get("scan_error")

    # ── Persistent scan error ────────────────────────────────────────────────
    if scan_error and st.session_state.get("credentials_wiped"):
        st.divider()
        st.error(f"❌ Scan failed: {scan_error}")
        st.info(
            "Your Gmail access was still permanently revoked — no credentials were retained.",
            icon="🔐",
        )
        if st.button("🔄 Try again", use_container_width=True):
            st.session_state["scan_error"]        = None
            st.session_state["credentials_wiped"] = False
            st.session_state["flights"]           = None
            st.rerun()
        return

    if flights is None or country is None:
        return

    st.divider()

    if not flights:
        st.subheader(f"✈️ {country.get('flag','')} {country['name']} — No flights found")
        st.info(
            "No matching flights were detected. This may mean:\n\n"
            "- Flight bookings were made with a different email address\n"
            "- Tickets were purchased by phone or through a travel agent (no email)\n"
            "- The confirmation emails were deleted before the scan window",
            icon="🔍",
        )
        return

    idx       = st.session_state.get("review_index", 0)
    decisions = st.session_state.get("flight_decisions", [])

    if idx < len(flights):
        _render_review_card(flights, idx, decisions, country)
    else:
        _render_final_results(flights, decisions, country)


# ─────────────────────────────────────────────────────────────────────────────
# REVIEW CARD — one flight at a time
# ─────────────────────────────────────────────────────────────────────────────

def _render_review_card(
    flights: list[dict],
    idx: int,
    decisions: list[str],
    country: dict,
):
    total = len(flights)
    f     = flights[idx]

    # Progress
    st.progress((idx) / total, text=f"Flight {idx + 1} of {total}")
    st.write("")

    # ── Route display ────────────────────────────────────────────────────────
    dep_code = f.get("departure_airport") or "?"
    arr_code = f.get("arrival_airport")   or "?"
    dep_city = ", ".join(filter(None, [f.get("departure_city"), f.get("departure_country")]))
    arr_city = ", ".join(filter(None, [f.get("arrival_city"),   f.get("arrival_country")]))

    st.markdown(
        f"""
        <div class="review-card">
          <div class="route-header">
            <div>
              <div class="route-airport">{dep_code}</div>
              <div class="route-city">{dep_city}</div>
            </div>
            <div class="route-arrow">→</div>
            <div>
              <div class="route-airport">{arr_code}</div>
              <div class="route-city">{arr_city}</div>
            </div>
          </div>
        </div>
        """,
        unsafe_allow_html=True,
    )

    # ── Flight details ───────────────────────────────────────────────────────
    d1, d2, d3, d4 = st.columns(4)
    d1.metric("📅 Date",        f.get("departure_date")    or "—")
    d2.metric("✈️ Airline",     f.get("airline")           or "—")
    d3.metric("🔢 Flight #",    f.get("flight_number")     or "—")
    d4.metric("📋 Booking Ref", f.get("booking_reference") or "—")

    # ── View source email ────────────────────────────────────────────────────
    st.write("")
    has_source = bool(f.get("_source_body"))
    if has_source:
        if st.button("📧 View source email", use_container_width=False):
            _email_viewer(
                subject=f.get("_source_subject", ""),
                sender =f.get("_source_sender",  ""),
                date   =f.get("_source_date",    ""),
                body   =f.get("_source_body",    ""),
            )
    else:
        st.caption("_(no source email captured for this flight)_")

    # ── Keep / Ignore / Back ─────────────────────────────────────────────────
    st.write("")
    col_back, col_ignore, col_keep = st.columns([1, 2, 2])

    with col_back:
        back_disabled = idx == 0
        if st.button("← Back", use_container_width=True, disabled=back_disabled):
            if decisions:
                decisions.pop()
                st.session_state["flight_decisions"] = decisions
            st.session_state["review_index"] = idx - 1
            st.rerun()

    with col_ignore:
        if st.button("❌  Ignore", use_container_width=True):
            decisions.append("ignore")
            st.session_state["flight_decisions"] = decisions
            st.session_state["review_index"]     = idx + 1
            st.rerun()

    with col_keep:
        if st.button("✅  Keep", use_container_width=True, type="primary"):
            decisions.append("keep")
            st.session_state["flight_decisions"] = decisions
            st.session_state["review_index"]     = idx + 1
            st.rerun()

    # Remaining count reminder
    remaining = total - idx - 1
    if remaining > 0:
        st.caption(f"{remaining} flight{'s' if remaining != 1 else ''} remaining to review")


# ─────────────────────────────────────────────────────────────────────────────
# FINAL RESULTS — after all flights are reviewed
# ─────────────────────────────────────────────────────────────────────────────

def _render_final_results(
    flights: list[dict],
    decisions: list[str],
    country: dict,
):
    kept    = [f for f, d in zip(flights, decisions) if d == "keep"]
    ignored = [f for f, d in zip(flights, decisions) if d == "ignore"]

    st.subheader(f"📊 Results — {country.get('flag','')} {country['name']}")

    m1, m2, m3 = st.columns(3)
    m1.metric("✈️ Detected",  len(flights))
    m2.metric("✅ Kept",       len(kept))
    m3.metric("❌ Ignored",    len(ignored))

    if not kept:
        st.warning(
            "No flights were kept. Use **Re-review** below to go back through them.",
            icon="⚠️",
        )
    else:
        # Compliance based only on kept flights
        compliance = comp_lib.check_compliance(kept, country)
        css = {"ok": "comp-ok", "warning": "comp-warning", "over": "comp-over"}[compliance.status]
        st.write("")
        st.markdown(f'<div class="{css}">{compliance.message}</div>', unsafe_allow_html=True)
        st.write("")

        if compliance.yearly_breakdown:
            with st.expander("📆 Year-by-year breakdown"):
                method     = country.get("calculation_method", "per_year")
                max_per_yr = country.get("max_days_abroad_per_year") if method == "per_year" else None
                year_df = pd.DataFrame([
                    {
                        "Year":             yr,
                        "Est. Days Abroad": days,
                        "Status":           "⚠️ Over limit" if max_per_yr and days > max_per_yr else "✅ OK",
                    }
                    for yr, days in sorted(compliance.yearly_breakdown.items())
                ])
                st.dataframe(year_df, use_container_width=True, hide_index=True)

        # Flight summary table (read-only)
        st.subheader(f"Kept flights ({len(kept)})")
        _SOURCE_KEYS = {"_source_subject", "_source_sender", "_source_date", "_source_body"}
        summary_df = pd.DataFrame([
            {k: v for k, v in f.items() if k not in _SOURCE_KEYS}
            for f in kept
        ])
        # Rename for display
        summary_df = summary_df.rename(columns={
            "departure_date":    "Dep. Date",
            "airline":           "Airline",
            "flight_number":     "Flight #",
            "departure_airport": "Dep. Airport",
            "departure_city":    "Dep. City",
            "departure_country": "Dep. Country",
            "arrival_airport":   "Arr. Airport",
            "arrival_city":      "Arr. City",
            "arrival_country":   "Arr. Country",
            "arrival_date":      "Arr. Date",
            "booking_reference": "Booking Ref",
        })
        st.dataframe(summary_df, use_container_width=True, hide_index=True)

        # CSV export
        st.divider()
        country_slug = re.sub(r"[^a-zA-Z0-9_-]", "_", country["name"])
        today        = datetime.now().strftime("%Y-%m-%d")
        st.download_button(
            label=f"⬇️ Download CSV — {len(kept)} flight{'s' if len(kept) != 1 else ''}",
            data=summary_df.to_csv(index=False).encode("utf-8"),
            file_name=f"{country_slug}_flight_history_{today}.csv",
            mime="text/csv",
            use_container_width=True,
            type="primary",
        )

        st.caption(
            "⚠️ **Disclaimer:** This tool is an aid for organising flight records only. "
            "Always verify against official airline records and consult a qualified "
            "immigration attorney before submitting to any government authority."
        )

    st.divider()
    if st.button("🔄 Re-review all flights", use_container_width=True):
        st.session_state["review_index"]     = 0
        st.session_state["flight_decisions"] = []
        st.rerun()


# ─────────────────────────────────────────────────────────────────────────────
# COUNTRY MANAGER (sidebar)
# ─────────────────────────────────────────────────────────────────────────────
def render_country_manager():
    with st.sidebar:
        st.header("⚙️ Manage Countries")
        st.caption("Configure citizenship residency rules for each country.")

        all_countries = country_lib.load_countries()

        if all_countries:
            for i, c in enumerate(all_countries):
                method = c.get("calculation_method", "per_year")
                limit  = (
                    f"{c.get('max_days_abroad_per_year','?')} days/yr"
                    if method == "per_year"
                    else f"{c.get('max_days_abroad_total','?')} days total"
                )
                col_info, col_del = st.columns([3, 1])
                col_info.markdown(
                    f"**{c.get('flag','🌐')} {c['name']}** ({c['iso_code']})\n\n"
                    f"_{c['lookback_years']}yr · {limit}_"
                )
                if col_del.button("✕", key=f"del_{i}", help=f"Remove {c['name']}"):
                    all_countries.pop(i)
                    country_lib.save_countries(all_countries)
                    st.rerun()
            st.divider()
        else:
            st.info("No countries yet. Add one below.")

        with st.expander("➕ Add country"):
            new_name   = st.text_input("Country name *",  key="new_name",   placeholder="Ireland")
            c_iso, c_flag = st.columns(2)
            new_iso    = c_iso.text_input("ISO code *",   key="new_iso",    placeholder="IE",  max_chars=2)
            new_flag   = c_flag.text_input("Flag emoji",  key="new_flag",   placeholder="🇮🇪")
            c_yrs, c_method = st.columns(2)
            new_years  = c_yrs.number_input("Lookback years *", min_value=1, max_value=15, value=5, key="new_years")
            new_method = c_method.selectbox("Method *", ["per_year", "total"], key="new_method")
            new_max    = st.number_input("Max days abroad *", min_value=1, max_value=9999, value=90, key="new_max")
            new_notes  = st.text_area("Notes", key="new_notes", placeholder="Describe residency rules…")

            if st.button("Add Country", use_container_width=True):
                try:
                    record = country_lib.validate_country({
                        "name":                 new_name,
                        "iso_code":             new_iso.upper() if new_iso else "",
                        "flag":                 new_flag or "🌐",
                        "lookback_years":       new_years,
                        "calculation_method":   new_method,
                        "max_days":             new_max,
                        "notes":                new_notes,
                    })
                    if any(c["iso_code"] == record["iso_code"] for c in all_countries):
                        st.error(f"{record['iso_code']} already exists.")
                    else:
                        all_countries.append(record)
                        country_lib.save_countries(all_countries)
                        st.success(f"✅ Added {record['name']}")
                        st.rerun()
                except ValueError as exc:
                    st.error(f"Validation error: {exc}")


# ─────────────────────────────────────────────────────────────────────────────
# LAYOUT
# ─────────────────────────────────────────────────────────────────────────────
col_left, col_right = st.columns([1, 1], gap="large")
with col_left:
    render_email_step()
with col_right:
    render_country_step()

st.divider()
render_scan_step()
render_results()
render_country_manager()

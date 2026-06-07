"""
Citizenship Flight Checker — Streamlit Application

Securely scans Gmail for international flight history to support citizenship
applications. Uses Google OAuth (read-only) + Claude AI extraction.

Security model summary:
  - Each Streamlit user has fully isolated st.session_state — no shared singletons.
  - OAuth state validated per-session to prevent CSRF (Fix C-2).
  - Fresh Credentials object created per API call (Fix C-1).
  - All inputs validated before use (Fix H-1, H-2).
  - No email content stored — processed in RAM only, sent to Anthropic API
    (disclosed to user before scan).
  - Country config writes require authentication (Fix C-3).
  - Atomic file writes prevent config corruption (Fix M-1).
"""

import io
import os
import re
from datetime import datetime

import pandas as pd
import streamlit as st
from dotenv import load_dotenv

from utils import auth, countries as country_lib, compliance as comp_lib, scanner

load_dotenv()

# ── Page Config ───────────────────────────────────────────────────────────────
st.set_page_config(
    page_title="Citizenship Flight Checker",
    page_icon="✈️",
    layout="wide",
    initial_sidebar_state="collapsed",
    menu_items={
        "About": (
            "**Citizenship Flight Checker**\n\n"
            "Securely scan Gmail flight history for citizenship applications.\n\n"
            "⚠️ This tool is an aid only. Always verify records with official "
            "airline documentation and consult a qualified immigration attorney."
        )
    },
)

# ── Custom CSS ────────────────────────────────────────────────────────────────
st.markdown(
    """
    <style>
    /* Tighten default Streamlit padding on mobile */
    .block-container { padding-top: 1.5rem; padding-bottom: 3rem; }
    /* Step card style */
    .step-header { font-size: 0.72rem; font-weight: 700; text-transform: uppercase;
                   letter-spacing: 0.08em; color: #64748b; margin-bottom: 0.25rem; }
    /* Compliance boxes */
    .comp-ok      { background:#d1fae5; border:1.5px solid #6ee7b7;
                    border-radius:8px; padding:12px 16px; color:#065f46; }
    .comp-warning { background:#fef3c7; border:1.5px solid #fbbf24;
                    border-radius:8px; padding:12px 16px; color:#92400e; }
    .comp-over    { background:#fee2e2; border:1.5px solid #fca5a5;
                    border-radius:8px; padding:12px 16px; color:#991b1b; }
    /* Hide Streamlit branding (optional) */
    #MainMenu { visibility: hidden; }
    footer     { visibility: hidden; }
    </style>
    """,
    unsafe_allow_html=True,
)

# ── State Initialisation ──────────────────────────────────────────────────────
def _init_state():
    defaults = {
        "authenticated":    False,
        "gmail_creds":      None,
        "connected_at":     None,
        "oauth_state":      None,
        "selected_country": None,
        "flights":          None,
        "scan_stats":       {},
        "acknowledged_privacy": False,
    }
    for k, v in defaults.items():
        if k not in st.session_state:
            st.session_state[k] = v

_init_state()

# ── OAuth Callback Handler (must run before any rendering) ────────────────────
def _check_oauth_callback():
    """Detect OAuth redirect and exchange code for tokens."""
    params = st.query_params
    if "code" not in params or "state" not in params:
        return

    code = params["code"]
    state = params["state"]

    # Clear params from URL immediately (Fix I-4 — don't leak code in Referer)
    st.query_params.clear()

    try:
        auth.handle_oauth_callback(code, state)
        st.success("✅ Gmail connected successfully.")
    except ValueError as exc:
        st.error(f"❌ Authentication failed: {exc}")
    except Exception as exc:
        st.error(f"❌ Unexpected error during login: {exc}")

_check_oauth_callback()


# ─────────────────────────────────────────────────────────────────────────────
# HEADER
# ─────────────────────────────────────────────────────────────────────────────
def render_header():
    col_logo, col_auth = st.columns([3, 1])
    with col_logo:
        st.markdown("## ✈️ Citizenship Flight Checker")
        st.caption("Residency travel analysis tool for citizenship applications")
    with col_auth:
        if auth.is_authenticated():
            st.success(f"🔒 Gmail connected", icon="✅")
            if st.button("Disconnect Gmail", key="logout_btn", use_container_width=True):
                auth.logout()
                st.session_state["flights"] = None
                st.rerun()

render_header()
st.divider()


# ─────────────────────────────────────────────────────────────────────────────
# STEP 1 — COUNTRY SELECTION
# ─────────────────────────────────────────────────────────────────────────────
def render_country_step():
    st.markdown('<p class="step-header">Step 1 — Select Country</p>', unsafe_allow_html=True)
    all_countries = country_lib.load_countries()

    if not all_countries:
        st.warning("No countries configured. Add one using **Manage Countries** below.")
        return

    options = {f"{c.get('flag','🌐')} {c['name']}": c for c in all_countries}
    choice = st.selectbox(
        "Applying for citizenship in:",
        options=list(options.keys()),
        index=None,
        placeholder="— Select a country —",
        key="country_select",
    )

    if choice:
        c = options[choice]
        st.session_state["selected_country"] = c

        method = c.get("calculation_method", "per_year")
        if method == "per_year":
            max_days_label = f"{c.get('max_days_abroad_per_year', '?')} days / year"
            calc_label = "Per year"
        else:
            max_days_label = f"{c.get('max_days_abroad_total', '?')} days total"
            calc_label = "Over full period"

        col1, col2, col3 = st.columns(3)
        col1.metric("Lookback Period", f"{c['lookback_years']} years")
        col2.metric("Max Days Abroad", max_days_label)
        col3.metric("Calculation", calc_label)

        if c.get("notes"):
            st.info(c["notes"], icon="ℹ️")
    else:
        st.session_state["selected_country"] = None


# ─────────────────────────────────────────────────────────────────────────────
# STEP 2 — GMAIL CONNECTION
# ─────────────────────────────────────────────────────────────────────────────
def render_gmail_step():
    st.markdown('<p class="step-header">Step 2 — Connect Gmail</p>', unsafe_allow_html=True)

    if auth.is_authenticated():
        st.success(
            f"Gmail connected at {st.session_state.get('connected_at', '')}",
            icon="🔒",
        )
        st.caption("Read-only access · No emails stored · Disconnect any time above")
        return

    # Privacy disclosure — must be acknowledged before OAuth (Fix L-4)
    with st.expander("📋 Privacy notice — read before connecting", expanded=not st.session_state["acknowledged_privacy"]):
        st.markdown(
            """
**What this app accesses:**
- ✅ Reads flight confirmation emails (Gmail read-only scope)

**What this app does NOT do:**
- 🚫 Send, delete, or modify any email
- 🚫 Store your emails on any server
- 🚫 Retain or log email content after processing

**Third-party AI processing:**
Email text is sent to **Anthropic's Claude API** for flight data extraction.
Anthropic's data handling terms apply to that transmission.
Your email content is not stored by this application.

**Tokens:** OAuth access tokens are stored only in your browser session
and discarded when you disconnect or close the browser.
            """
        )
        st.session_state["acknowledged_privacy"] = st.checkbox(
            "I understand — email content will be sent to Anthropic's API for extraction",
            value=st.session_state["acknowledged_privacy"],
        )

    if not st.session_state["acknowledged_privacy"]:
        st.button("Connect Gmail", disabled=True, use_container_width=True, key="connect_disabled")
        st.caption("Please acknowledge the privacy notice above first.")
        return

    try:
        auth_url = auth.get_auth_url()
        st.link_button(
            "🔑 Connect Gmail (read-only)",
            url=auth_url,
            use_container_width=True,
            type="primary",
        )
        st.caption(
            "You'll be redirected to Google to grant **read-only** Gmail access. "
            "We never see your password."
        )
    except EnvironmentError as exc:
        st.error(f"Configuration error: {exc}")


# ─────────────────────────────────────────────────────────────────────────────
# STEP 3 — SCAN
# ─────────────────────────────────────────────────────────────────────────────
def render_scan_step():
    st.markdown('<p class="step-header">Step 3 — Scan Emails</p>', unsafe_allow_html=True)

    country = st.session_state.get("selected_country")
    ready = auth.is_authenticated() and country is not None

    if not ready:
        st.button(
            "✈️ Start Email Scan",
            disabled=True,
            use_container_width=True,
            key="scan_disabled",
            help="Select a country and connect Gmail first.",
        )
        return

    st.info(
        f"Will search emails going back **{country['lookback_years']} years** "
        f"for flights involving **{country.get('flag','')} {country['name']}**.",
        icon="⏱️",
    )

    if st.button("✈️ Start Email Scan", type="primary", use_container_width=True, key="scan_btn"):
        _run_scan(country)


def _run_scan(country: dict):
    """Execute the email scan with real-time progress updates."""
    progress_bar = st.progress(0, text="Connecting to Gmail…")
    status_text = st.empty()

    flights_result = []
    stats = {}

    def on_progress(processed: int, total: int, message: str):
        pct = int((processed / total) * 100) if total > 0 else 0
        progress_bar.progress(pct, text=message)
        status_text.caption(f"Emails processed: {processed} / {total}")

    try:
        creds = auth.get_credentials()

        # Validate inputs against allowlist before use (Fix H-2, H-1)
        validated_country = country_lib.get_country_by_iso(country["iso_code"])
        if not validated_country:
            st.error(f"Unknown country code: {country['iso_code']!r}")
            return

        lookback_years = int(validated_country["lookback_years"])
        if not (1 <= lookback_years <= 15):
            st.error("Invalid lookback period.")
            return

        flights_result = scanner.scan_emails(
            credentials=creds,
            country_iso_code=validated_country["iso_code"],
            lookback_years=lookback_years,
            progress_callback=on_progress,
        )

        stats["total_found"] = len(flights_result)

    except Exception as exc:
        progress_bar.empty()
        status_text.empty()
        st.error(f"❌ Scan failed: {exc}")
        return

    progress_bar.progress(100, text=f"Done! Found {len(flights_result)} flights.")
    status_text.empty()

    st.session_state["flights"] = flights_result
    st.session_state["scan_stats"] = stats
    st.rerun()


# ─────────────────────────────────────────────────────────────────────────────
# RESULTS
# ─────────────────────────────────────────────────────────────────────────────
def render_results():
    flights = st.session_state.get("flights", [])
    country = st.session_state.get("selected_country")
    if flights is None or country is None:
        return

    st.divider()
    st.subheader("📊 Results")

    # ── Summary metrics ──────────────────────────────────────────────────────
    compliance = comp_lib.check_compliance(flights, country)
    periods = compliance.periods
    total_days = compliance.total_days

    dates = sorted([f["departure_date"] for f in flights if f.get("departure_date")])
    date_range = f"{dates[0]} → {dates[-1]}" if len(dates) >= 2 else (dates[0] if dates else "—")

    m1, m2, m3, m4 = st.columns(4)
    m1.metric("✈️ Flights Found",    len(flights))
    m2.metric("🌍 Abroad Trips",     len(periods))
    m3.metric("📅 Days Abroad (est.)", total_days)
    m4.metric("🗓️ Date Range",       date_range)

    # ── Compliance alert ─────────────────────────────────────────────────────
    css_class = {"ok": "comp-ok", "warning": "comp-warning", "over": "comp-over"}[compliance.status]
    st.markdown(
        f'<div class="{css_class}">{compliance.message}</div>',
        unsafe_allow_html=True,
    )
    st.caption("")  # Spacer

    # ── Per-year breakdown (collapsible) ─────────────────────────────────────
    if compliance.yearly_breakdown:
        with st.expander("📆 Year-by-year breakdown"):
            yd = compliance.yearly_breakdown
            method = country.get("calculation_method", "per_year")
            max_per_year = country.get("max_days_abroad_per_year") if method == "per_year" else None

            year_df = pd.DataFrame(
                [
                    {
                        "Year": yr,
                        "Days Abroad (est.)": days,
                        "Status": (
                            "⚠️ Over limit" if max_per_year and days > max_per_year
                            else "✅ OK"
                        ),
                    }
                    for yr, days in sorted(yd.items())
                ]
            )
            st.dataframe(year_df, use_container_width=True, hide_index=True)

    # ── Flight table ─────────────────────────────────────────────────────────
    st.subheader(
        f"International Flight History — {country.get('flag','')} {country['name']}",
    )

    if not flights:
        st.info(
            "No flights found involving this country in the scanned emails. "
            "This may mean flights were booked through channels not in your Gmail "
            "(phone calls, travel agencies, work booking tools).",
            icon="🔍",
        )
        return

    def direction(f: dict) -> str:
        if f.get("departure_country") == country["iso_code"] and f.get("arrival_country") != country["iso_code"]:
            return "🛫 Departure"
        if f.get("arrival_country") == country["iso_code"] and f.get("departure_country") != country["iso_code"]:
            return "🛬 Return"
        return "↔️ Other"

    df = pd.DataFrame([
        {
            "Departure Date":     f.get("departure_date") or "—",
            "Airline":            f.get("airline") or "—",
            "Flight #":           f.get("flight_number") or "—",
            "Departed From":      f"{f.get('departure_airport') or ''} {f.get('departure_city') or ''}".strip() or "—",
            "Dep. Country":       f.get("departure_country") or "—",
            "Arrived At":         f"{f.get('arrival_airport') or ''} {f.get('arrival_city') or ''}".strip() or "—",
            "Arr. Country":       f.get("arrival_country") or "—",
            "Arrival Date":       f.get("arrival_date") or "—",
            "Direction":          direction(f),
            "Booking Ref":        f.get("booking_reference") or "—",
        }
        for f in flights
    ])

    # Search filter
    search = st.text_input("🔍 Filter flights", placeholder="Search by city, airline, flight number…")
    if search:
        mask = df.apply(lambda row: row.astype(str).str.contains(search, case=False).any(), axis=1)
        display_df = df[mask]
    else:
        display_df = df

    st.dataframe(
        display_df,
        use_container_width=True,
        hide_index=True,
        column_config={
            "Direction": st.column_config.TextColumn(width="medium"),
        },
    )
    st.caption(f"Showing {len(display_df)} of {len(df)} flights")

    # ── CSV Download ─────────────────────────────────────────────────────────
    country_name = re.sub(r"[^a-zA-Z0-9_-]", "_", country["name"])
    today = datetime.now().strftime("%Y-%m-%d")
    filename = f"{country_name}_flight_history_{today}.csv"

    csv_bytes = df.to_csv(index=False).encode("utf-8")
    st.download_button(
        label=f"⬇️ Download CSV ({len(flights)} flights)",
        data=csv_bytes,
        file_name=filename,
        mime="text/csv",
        use_container_width=True,
        type="secondary",
    )

    st.caption(
        "⚠️ **Disclaimer:** This tool aids in organizing flight records. "
        "Always verify against official airline records and consult a qualified "
        "immigration attorney before submitting to any government authority."
    )


# ─────────────────────────────────────────────────────────────────────────────
# COUNTRY MANAGER (sidebar)
# ─────────────────────────────────────────────────────────────────────────────
def render_country_manager():
    with st.sidebar:
        st.header("⚙️ Manage Countries")
        st.caption("Configure citizenship requirements for each country.")

        all_countries = country_lib.load_countries()

        # ── Existing countries ────────────────────────────────────────────────
        if all_countries:
            for i, c in enumerate(all_countries):
                method = c.get("calculation_method", "per_year")
                if method == "per_year":
                    limit = f"{c.get('max_days_abroad_per_year','?')} days/yr"
                else:
                    limit = f"{c.get('max_days_abroad_total','?')} days total"

                col_info, col_del = st.sidebar.columns([3, 1])
                col_info.markdown(
                    f"**{c.get('flag','🌐')} {c['name']}** ({c['iso_code']})\n\n"
                    f"_{c['lookback_years']}yr · {limit}_"
                )
                # Fix C-3: delete gated behind authentication
                if col_del.button("✕", key=f"del_{i}", help=f"Remove {c['name']}"):
                    if not auth.is_authenticated():
                        st.sidebar.error("Please connect Gmail before modifying countries.")
                    else:
                        all_countries.pop(i)
                        country_lib.save_countries(all_countries)
                        st.sidebar.success(f"Removed {c['name']}")
                        st.rerun()

            st.sidebar.divider()
        else:
            st.sidebar.info("No countries configured yet.")

        # ── Add new country ────────────────────────────────────────────────────
        with st.sidebar.expander("➕ Add new country"):
            new_name = st.text_input("Country name *", key="new_name", placeholder="Ireland")
            col_iso, col_flag = st.columns(2)
            new_iso = col_iso.text_input("ISO code *", key="new_iso", placeholder="IE", max_chars=2)
            new_flag = col_flag.text_input("Flag emoji", key="new_flag", placeholder="🇮🇪")

            col_yrs, col_method = st.columns(2)
            new_years = col_yrs.number_input("Lookback years *", min_value=1, max_value=15, value=5, key="new_years")
            new_method = col_method.selectbox("Calculation *", ["per_year", "total"], key="new_method")
            new_max = st.number_input("Max days abroad *", min_value=1, max_value=9999, value=90, key="new_max")
            new_notes = st.text_area("Notes", key="new_notes", placeholder="Describe residency rules…")

            if st.button("Add Country", key="add_country_btn", use_container_width=True):
                # Fix C-3: writes require authentication
                if not auth.is_authenticated():
                    st.sidebar.error("Please connect Gmail before modifying countries.")
                else:
                    try:
                        record = country_lib.validate_country({
                            "name": new_name,
                            "iso_code": new_iso.upper(),
                            "flag": new_flag or "🌐",
                            "lookback_years": new_years,
                            "calculation_method": new_method,
                            "max_days": new_max,
                            "notes": new_notes,
                        })
                        existing_isos = {c["iso_code"] for c in all_countries}
                        if record["iso_code"] in existing_isos:
                            st.sidebar.error(f"{record['iso_code']} already exists.")
                        else:
                            all_countries.append(record)
                            country_lib.save_countries(all_countries)
                            st.sidebar.success(f"✅ Added {record['name']}")
                            st.rerun()
                    except ValueError as exc:
                        st.sidebar.error(f"Validation error: {exc}")

        st.sidebar.divider()
        st.sidebar.caption(
            "🔒 Country configuration changes require Gmail connection.\n\n"
            "Countries are stored in `config/countries.json`."
        )


# ─────────────────────────────────────────────────────────────────────────────
# MAIN LAYOUT
# ─────────────────────────────────────────────────────────────────────────────
col_left, col_right = st.columns([1, 1], gap="large")

with col_left:
    render_country_step()

with col_right:
    render_gmail_step()

st.divider()
render_scan_step()
render_results()
render_country_manager()

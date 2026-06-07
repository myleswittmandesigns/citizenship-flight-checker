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
    #MainMenu { visibility: hidden; }
    footer     { visibility: hidden; }
    </style>
    """,
    unsafe_allow_html=True,
)

# ── Session state defaults ────────────────────────────────────────────────────
_DEFAULTS = {
    "authenticated":     False,
    "imap_creds":        None,
    "connected_email":   None,
    "selected_country":  None,
    "flights":           None,
    "scan_stats":        {},
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
        st.success(f"Connected", icon="🔒")
        st.caption(email_display)
        if st.button("Disconnect", use_container_width=True):
            auth.logout()
            st.session_state["flights"] = None
            st.rerun()

st.divider()


# ─────────────────────────────────────────────────────────────────────────────
# STEP 1 — SELECT COUNTRY
# ─────────────────────────────────────────────────────────────────────────────
def render_country_step():
    st.markdown('<p class="step-label">Step 1 — Select Country</p>', unsafe_allow_html=True)

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


# ─────────────────────────────────────────────────────────────────────────────
# STEP 2 — CONNECT EMAIL (IMAP)
# ─────────────────────────────────────────────────────────────────────────────
def render_email_step():
    st.markdown('<p class="step-label">Step 2 — Connect Your Email</p>', unsafe_allow_html=True)

    if auth.is_authenticated():
        st.success(
            f"✅ Connected as **{st.session_state.get('connected_email','')}**\n\n"
            "Your app password is held in memory only and will be cleared when you disconnect.",
            icon="🔒",
        )
        return

    # ── Provider selector ────────────────────────────────────────────────────
    provider_name = st.selectbox(
        "Email provider",
        options=list(auth.PROVIDERS.keys()),
        index=0,
    )
    provider = auth.PROVIDERS[provider_name]

    # ── App password instructions ─────────────────────────────────────────────
    with st.expander(f"📋 How to get an app password for {provider_name}", expanded=True):
        st.markdown(provider["app_password_steps"])
        if provider.get("app_password_url"):
            st.link_button(
                f"Open {provider_name} security settings →",
                url=provider["app_password_url"],
            )
        st.caption(
            "An **app password** is a one-time code your provider generates specifically "
            "for third-party apps. It grants limited access and can be revoked any time "
            "from your account settings — without changing your main password."
        )

    # ── Credential inputs ────────────────────────────────────────────────────
    email_addr = st.text_input(
        "Email address",
        placeholder="you@example.com",
        autocomplete="email",
    )

    # Custom host/port for "Other" provider
    if provider_name == "Other (custom IMAP)":
        col_host, col_port = st.columns([3, 1])
        custom_host = col_host.text_input("IMAP server", placeholder="imap.yourprovider.com")
        custom_port = col_port.number_input("Port", value=993, min_value=1, max_value=65535)
        host = custom_host
        port = int(custom_port)
        folders = ["INBOX"]
    else:
        host = provider["host"]
        port = provider["port"]
        folders = provider["folders"]

    app_password = st.text_input(
        "App password",
        type="password",
        placeholder="xxxx xxxx xxxx xxxx",
        help="Use the app password generated by your email provider — not your regular login password.",
    )

    st.caption(
        "🔒 Your app password is never stored on disk or sent anywhere "
        "except directly to your email provider's IMAP server over an encrypted connection."
    )

    if st.button("Connect Email", type="primary", use_container_width=True):
        if not email_addr or not app_password:
            st.error("Please enter both your email address and app password.")
            return
        if provider_name == "Other (custom IMAP)" and not host:
            st.error("Please enter your IMAP server address.")
            return

        with st.spinner("Testing connection…"):
            ok, message = auth.test_connection(host, port, email_addr, app_password)

        if ok:
            auth.save_credentials(email_addr, app_password, host, port, folders)
            st.success("✅ Connected successfully!")
            st.rerun()
        else:
            st.error(f"❌ {message}")


# ─────────────────────────────────────────────────────────────────────────────
# STEP 3 — SCAN
# ─────────────────────────────────────────────────────────────────────────────
def render_scan_step():
    st.markdown('<p class="step-label">Step 3 — Scan for Flights</p>', unsafe_allow_html=True)

    country = st.session_state.get("selected_country")
    ready = auth.is_authenticated() and country is not None

    if not ready:
        missing = []
        if not country:
            missing.append("select a country (Step 1)")
        if not auth.is_authenticated():
            missing.append("connect your email (Step 2)")
        st.button(
            "✈️ Start Scan",
            disabled=True,
            use_container_width=True,
            help="Please " + " and ".join(missing) + " first.",
        )
        return

    st.info(
        f"Will search **{country['lookback_years']} years** of email for flights "
        f"involving **{country.get('flag','')} {country['name']}** "
        f"using Claude AI to read confirmation emails.",
        icon="⏱️",
    )

    # Privacy notice
    st.caption(
        "📋 Email text is sent to Anthropic's Claude API for flight extraction. "
        "Emails are not stored by this app. "
        "[Anthropic privacy policy](https://www.anthropic.com/privacy)"
    )

    if st.button("✈️ Start Email Scan", type="primary", use_container_width=True):
        _run_scan(country)


def _run_scan(country: dict):
    progress_bar = st.progress(0, text="Connecting to email server…")
    status_text  = st.empty()

    def on_progress(processed: int, total: int, message: str):
        pct = int((processed / total) * 100) if total > 0 else 0
        progress_bar.progress(pct, text=message)
        status_text.caption(f"{processed} / {total} emails analyzed")

    try:
        creds = auth.get_credentials()

        validated = country_lib.get_country_by_iso(country["iso_code"])
        if not validated:
            st.error(f"Unknown country code: {country['iso_code']!r}")
            return

        lookback = int(validated["lookback_years"])
        if not (1 <= lookback <= 15):
            st.error("Invalid lookback period in country configuration.")
            return

        flights = scanner.scan_emails(
            creds=creds,
            country_iso_code=validated["iso_code"],
            lookback_years=lookback,
            progress_callback=on_progress,
        )

    except Exception as exc:
        progress_bar.empty()
        status_text.empty()
        st.error(f"❌ Scan failed: {exc}")
        return

    progress_bar.progress(100, text=f"Done — {len(flights)} flights found.")
    status_text.empty()
    st.session_state["flights"] = flights
    st.rerun()


# ─────────────────────────────────────────────────────────────────────────────
# RESULTS
# ─────────────────────────────────────────────────────────────────────────────
def render_results():
    flights = st.session_state.get("flights")
    country = st.session_state.get("selected_country")

    if flights is None or country is None:
        return

    st.divider()
    st.subheader("📊 Results")

    compliance = comp_lib.check_compliance(flights, country)
    periods    = compliance.periods
    total_days = compliance.total_days

    dates = sorted([f["departure_date"] for f in flights if f.get("departure_date")])
    date_range = f"{dates[0]} → {dates[-1]}" if len(dates) >= 2 else (dates[0] if dates else "—")

    m1, m2, m3, m4 = st.columns(4)
    m1.metric("✈️ Flights Found",         len(flights))
    m2.metric("🌍 Abroad Trips",          len(periods))
    m3.metric("📅 Est. Days Abroad",       total_days)
    m4.metric("🗓️ Date Range",             date_range)

    # Compliance alert
    css = {"ok": "comp-ok", "warning": "comp-warning", "over": "comp-over"}[compliance.status]
    st.markdown(f'<div class="{css}">{compliance.message}</div>', unsafe_allow_html=True)
    st.write("")

    # Year-by-year breakdown
    if compliance.yearly_breakdown:
        with st.expander("📆 Year-by-year breakdown"):
            method      = country.get("calculation_method", "per_year")
            max_per_yr  = country.get("max_days_abroad_per_year") if method == "per_year" else None
            year_df = pd.DataFrame([
                {
                    "Year":               yr,
                    "Est. Days Abroad":   days,
                    "Status":             "⚠️ Over limit" if max_per_yr and days > max_per_yr else "✅ OK",
                }
                for yr, days in sorted(compliance.yearly_breakdown.items())
            ])
            st.dataframe(year_df, use_container_width=True, hide_index=True)

    # Flight table
    st.subheader(f"Flight History — {country.get('flag','')} {country['name']}")

    if not flights:
        st.info(
            "No matching flights found. Flight bookings made by phone, through a travel agent, "
            "or with a different email address won't appear here.",
            icon="🔍",
        )
        return

    def direction(f: dict) -> str:
        dep = f.get("departure_country") == country["iso_code"]
        arr = f.get("arrival_country")   == country["iso_code"]
        if dep and not arr:
            return "🛫 Departure"
        if arr and not dep:
            return "🛬 Return"
        return "↔️ Other"

    df = pd.DataFrame([
        {
            "Departure Date":  f.get("departure_date") or "—",
            "Airline":         f.get("airline") or "—",
            "Flight #":        f.get("flight_number") or "—",
            "Departed From":   (f"{f.get('departure_airport') or ''} {f.get('departure_city') or ''}").strip() or "—",
            "Dep. Country":    f.get("departure_country") or "—",
            "Arrived At":      (f"{f.get('arrival_airport') or ''} {f.get('arrival_city') or ''}").strip() or "—",
            "Arr. Country":    f.get("arrival_country") or "—",
            "Arrival Date":    f.get("arrival_date") or "—",
            "Direction":       direction(f),
            "Booking Ref":     f.get("booking_reference") or "—",
        }
        for f in flights
    ])

    search = st.text_input("🔍 Filter", placeholder="Search by city, airline, flight number…")
    if search:
        mask = df.apply(lambda row: row.astype(str).str.contains(search, case=False).any(), axis=1)
        display_df = df[mask]
    else:
        display_df = df

    st.dataframe(display_df, use_container_width=True, hide_index=True)
    st.caption(f"Showing {len(display_df)} of {len(df)} flights")

    # CSV download
    country_slug = re.sub(r"[^a-zA-Z0-9_-]", "_", country["name"])
    today        = datetime.now().strftime("%Y-%m-%d")
    st.download_button(
        label=f"⬇️ Download CSV  ({len(flights)} flights)",
        data=df.to_csv(index=False).encode("utf-8"),
        file_name=f"{country_slug}_flight_history_{today}.csv",
        mime="text/csv",
        use_container_width=True,
    )

    st.caption(
        "⚠️ **Disclaimer:** This tool is an aid for organizing flight records only. "
        "Always verify against official airline records and consult a qualified "
        "immigration attorney before submitting to any government authority."
    )


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
    render_country_step()
with col_right:
    render_email_step()

st.divider()
render_scan_step()
render_results()
render_country_manager()

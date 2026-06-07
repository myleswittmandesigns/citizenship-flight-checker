"""
Google OAuth 2.0 authentication for Streamlit.

Security design (all audit findings addressed):
  - FIX C-1: No shared OAuth singleton. A fresh Flow/Credentials object is
    created per operation. st.session_state is fully isolated per user.
  - FIX C-2: OAuth `state` is stored in st.session_state before redirect and
    validated on callback with secrets.compare_digest(). Consumed exactly once.
  - FIX H-3: Secrets loaded from st.secrets or env — startup fails fast if missing.
  - FIX M-3: Tokens live only in st.session_state (per-user RAM). Never on disk.
  - FIX M-4: access_type='offline' so refresh tokens are issued and access
    tokens can be refreshed automatically.
  - Privacy guarantee: logout() wipes all token state — called after every scan.
"""

import os
import secrets
import streamlit as st
from google_auth_oauthlib.flow import Flow
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request

# Read-only Gmail scope — minimum necessary privilege
GMAIL_SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]


def _get_secret(key: str) -> str:
    """Load a secret from st.secrets (Streamlit Cloud) or env (local).
    Raises clearly if missing — no silent fallback defaults (Fix H-3).
    """
    try:
        val = st.secrets.get(key)
        if val:
            return val
    except Exception:
        pass
    val = os.getenv(key)
    if not val:
        raise EnvironmentError(
            f"Required secret '{key}' not found. "
            f"Add it to .streamlit/secrets.toml (local) or Streamlit Cloud secrets."
        )
    return val


def get_redirect_uri() -> str:
    try:
        uri = st.secrets.get("REDIRECT_URI")
    except Exception:
        uri = None
    return uri or os.getenv("REDIRECT_URI") or "http://localhost:8501"


def _make_flow() -> Flow:
    """Create a fresh OAuth2 Flow. Never reuse across users (Fix C-1)."""
    return Flow.from_client_config(
        {
            "web": {
                "client_id":     _get_secret("GOOGLE_CLIENT_ID"),
                "client_secret": _get_secret("GOOGLE_CLIENT_SECRET"),
                "auth_uri":      "https://accounts.google.com/o/oauth2/auth",
                "token_uri":     "https://oauth2.googleapis.com/token",
            }
        },
        scopes=GMAIL_SCOPES,
        redirect_uri=get_redirect_uri(),
    )


def get_auth_url() -> str:
    """Generate the Google sign-in URL and store CSRF state in session.

    State is cryptographically random, stored before redirect, validated
    on callback — prevents CSRF and token injection (Fix C-2).
    """
    flow  = _make_flow()
    state = secrets.token_urlsafe(32)
    st.session_state["oauth_state"] = state

    auth_url, _ = flow.authorization_url(
        access_type="offline",      # Issues refresh token (Fix M-4)
        prompt="select_account",    # Let user choose account; no forced re-consent
        state=state,
        include_granted_scopes="true",
    )
    return auth_url


def handle_oauth_callback(code: str, returned_state: str) -> None:
    """Exchange authorization code for tokens after validating CSRF state.

    Raises ValueError on any validation failure.
    Tokens stored only in st.session_state (Fix M-3, Fix C-1).
    """
    stored_state = st.session_state.pop("oauth_state", None)

    if stored_state is None:
        raise ValueError(
            "No OAuth state in session — possible expired session or CSRF attempt. "
            "Please start the sign-in flow again."
        )
    # Constant-time comparison prevents timing attacks on the state token
    if not secrets.compare_digest(stored_state, returned_state):
        raise ValueError(
            "OAuth state mismatch — possible CSRF attempt. "
            "Please start the sign-in flow again."
        )

    flow = _make_flow()
    flow.fetch_token(code=code)
    creds = flow.credentials

    # Store serialised credentials — never the Flow object (Fix C-1)
    st.session_state["google_creds"] = {
        "token":         creds.token,
        "refresh_token": creds.refresh_token,
        "token_uri":     creds.token_uri,
        "client_id":     creds.client_id,
        "client_secret": creds.client_secret,
        "scopes":        list(creds.scopes or GMAIL_SCOPES),
    }
    st.session_state["authenticated"]  = True
    st.session_state["connected_email"] = (
        st.session_state.get("google_user_email", "Gmail account")
    )


def get_credentials() -> Credentials:
    """Reconstruct a fresh Credentials object from session state (Fix C-1).
    Auto-refreshes the access token if expired (Fix M-4).
    """
    raw = st.session_state.get("google_creds")
    if not raw:
        raise RuntimeError("Not signed in. Please connect with Google first.")

    creds = Credentials(
        token=raw["token"],
        refresh_token=raw.get("refresh_token"),
        token_uri=raw["token_uri"],
        client_id=raw["client_id"],
        client_secret=raw["client_secret"],
        scopes=raw["scopes"],
    )

    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
        raw["token"] = creds.token
        st.session_state["google_creds"] = raw

    return creds


def logout() -> None:
    """Permanently wipe all auth state from session — called after every scan."""
    for key in ("google_creds", "authenticated", "connected_email",
                "oauth_state", "google_user_email"):
        st.session_state.pop(key, None)


def is_authenticated() -> bool:
    return bool(
        st.session_state.get("authenticated")
        and st.session_state.get("google_creds")
    )

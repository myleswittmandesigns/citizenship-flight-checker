"""
Gmail OAuth 2.0 handling for Streamlit.

Security design decisions vs. prior Express version:
  - FIX C-1: No shared OAuth singleton. A fresh Flow/Credentials object is
    created per operation. Streamlit session_state is per-user and isolated.
  - FIX C-2: OAuth `state` is stored in session_state before redirect and
    validated on callback. Mismatches raise immediately and consume the state.
  - FIX H-3: Secrets loaded from st.secrets (Streamlit Cloud) or .env
    (local), never from a fallback default.
  - FIX M-3: Tokens live only in st.session_state (per-user RAM). Never
    written to disk or a shared store.
  - FIX M-4: access_type='offline' requested so refresh tokens are issued.
    Credentials object handles refresh automatically.
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
    """Load a secret from st.secrets (Streamlit Cloud) or env (local dev).
    Raises clearly if not found — no silent fallback defaults.
    """
    # st.secrets is available in both Cloud and local .streamlit/secrets.toml
    try:
        val = st.secrets.get(key)
        if val:
            return val
    except Exception:
        pass
    val = os.getenv(key)
    if not val:
        raise EnvironmentError(
            f"Required secret '{key}' not found in st.secrets or environment. "
            f"Copy .env.example to .env and fill in your credentials."
        )
    return val


def _get_redirect_uri() -> str:
    try:
        uri = st.secrets.get("REDIRECT_URI") or os.getenv("REDIRECT_URI")
    except Exception:
        uri = os.getenv("REDIRECT_URI")
    return uri or "http://localhost:8501"


def _make_flow() -> Flow:
    """Create a fresh OAuth flow. Never reuse across users or requests."""
    client_config = {
        "web": {
            "client_id": _get_secret("GOOGLE_CLIENT_ID"),
            "client_secret": _get_secret("GOOGLE_CLIENT_SECRET"),
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
        }
    }
    return Flow.from_client_config(
        client_config,
        scopes=GMAIL_SCOPES,
        redirect_uri=_get_redirect_uri(),
    )


def get_auth_url() -> str:
    """Generate OAuth authorization URL and save CSRF state to session.

    The state value is cryptographically random and consumed exactly once
    on callback — preventing CSRF and replay attacks (Fix C-2).
    """
    flow = _make_flow()
    state = secrets.token_urlsafe(32)
    st.session_state["oauth_state"] = state

    auth_url, _ = flow.authorization_url(
        access_type="offline",    # Issues refresh token (Fix M-4)
        prompt="select_account",  # No forced re-consent on every login (Fix L-1)
        state=state,
        include_granted_scopes="true",
    )
    return auth_url


def handle_oauth_callback(code: str, returned_state: str) -> None:
    """Exchange authorization code for tokens after validating CSRF state.

    Raises ValueError on state mismatch — caller should surface this to user.
    Tokens are stored only in st.session_state (Fix M-3, Fix C-1).
    """
    stored_state = st.session_state.pop("oauth_state", None)

    if stored_state is None:
        raise ValueError(
            "No OAuth state found in session. "
            "This may indicate a CSRF attempt or an expired session."
        )
    if not secrets.compare_digest(stored_state, returned_state):
        raise ValueError(
            "OAuth state mismatch — possible CSRF attack. "
            "Please start the login flow again."
        )

    flow = _make_flow()
    # Exchange the authorization code for tokens
    flow.fetch_token(code=code)
    creds = flow.credentials

    # Store serialized credentials — never the Flow object (Fix C-1)
    st.session_state["gmail_creds"] = {
        "token": creds.token,
        "refresh_token": creds.refresh_token,
        "token_uri": creds.token_uri,
        "client_id": creds.client_id,
        "client_secret": creds.client_secret,
        "scopes": list(creds.scopes or GMAIL_SCOPES),
    }
    st.session_state["authenticated"] = True
    st.session_state["connected_at"] = str(
        __import__("datetime").datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    )


def get_credentials() -> Credentials:
    """Reconstruct a fresh Credentials object from session state.

    This is per-call (Fix C-1 — no shared singleton).
    Automatically refreshes the access token if expired (Fix M-4).
    """
    raw = st.session_state.get("gmail_creds")
    if not raw:
        raise RuntimeError("Not authenticated. Please connect Gmail first.")

    creds = Credentials(
        token=raw["token"],
        refresh_token=raw.get("refresh_token"),
        token_uri=raw["token_uri"],
        client_id=raw["client_id"],
        client_secret=raw["client_secret"],
        scopes=raw["scopes"],
    )

    # Refresh if expired — updates token in place
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
        # Persist refreshed token back to session
        raw["token"] = creds.token
        st.session_state["gmail_creds"] = raw

    return creds


def logout() -> None:
    """Clear all auth-related state from the session."""
    for key in ("gmail_creds", "authenticated", "connected_at", "oauth_state"):
        st.session_state.pop(key, None)


def is_authenticated() -> bool:
    return bool(st.session_state.get("authenticated") and st.session_state.get("gmail_creds"))

"""
Google OAuth 2.0 authentication for Streamlit.

Root cause of the session loss problem:
  Streamlit creates a NEW session when Google redirects back to the app —
  the WebSocket connection was dropped while the user was on Google's page.
  Any oauth_state stored in st.session_state before the redirect is gone.

Fix: HMAC-signed state tokens.
  Instead of storing state server-side and comparing on callback, we encode
  a cryptographically signed token as the state parameter. The signature can
  be verified on any session without storage — equivalent CSRF protection,
  zero session dependency.

Other security design:
  - FIX C-1: No shared OAuth singleton. Fresh Flow/Credentials per operation.
  - FIX H-3: Secrets loaded from st.secrets or env — fails fast if missing.
  - FIX M-3: Tokens live only in st.session_state (per-user RAM, never disk).
  - FIX M-4: access_type='offline' so refresh tokens are issued.
  - Privacy guarantee: logout() wipes all token state after every scan.
"""

import base64
import hashlib
import hmac
import os
import secrets
import streamlit as st
from google_auth_oauthlib.flow import Flow
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request

GMAIL_SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]


# ── Secrets ───────────────────────────────────────────────────────────────────

def _get_secret(key: str) -> str:
    """Load from st.secrets (Streamlit Cloud) or env (local). Fails fast."""
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
            f"Add it to .streamlit/secrets.toml or Streamlit Cloud secrets."
        )
    return val


def get_redirect_uri() -> str:
    try:
        uri = st.secrets.get("REDIRECT_URI")
    except Exception:
        uri = None
    return uri or os.getenv("REDIRECT_URI") or "http://localhost:8501"


# ── HMAC-signed state tokens ──────────────────────────────────────────────────
# These survive Streamlit's session recreation on redirect because they are
# self-verifying — no server-side storage needed.

def _make_signed_state() -> str:
    """Create a signed state token: base64(nonce) + '.' + base64(HMAC).

    The nonce provides uniqueness; the HMAC proves the token was issued by
    this server. Signing key is the GOOGLE_CLIENT_SECRET so it's secret and
    stable across Streamlit session boundaries.
    """
    nonce = secrets.token_bytes(32)
    nonce_b64 = base64.urlsafe_b64encode(nonce).decode().rstrip("=")
    key = _get_secret("GOOGLE_CLIENT_SECRET").encode()
    sig = hmac.new(key, nonce_b64.encode(), hashlib.sha256).digest()
    sig_b64 = base64.urlsafe_b64encode(sig).decode().rstrip("=")
    return f"{nonce_b64}.{sig_b64}"


def _verify_signed_state(state: str) -> bool:
    """Verify an HMAC-signed state token. Returns False on any failure."""
    try:
        parts = state.rsplit(".", 1)
        if len(parts) != 2:
            return False
        nonce_b64, received_sig_b64 = parts
        key = _get_secret("GOOGLE_CLIENT_SECRET").encode()
        expected_sig = hmac.new(key, nonce_b64.encode(), hashlib.sha256).digest()
        expected_b64 = base64.urlsafe_b64encode(expected_sig).decode().rstrip("=")
        return hmac.compare_digest(received_sig_b64, expected_b64)
    except Exception:
        return False


# ── OAuth Flow ────────────────────────────────────────────────────────────────

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
    """Generate the Google sign-in URL with a signed state token.

    The state is HMAC-signed so it can be verified on callback without
    session storage — survives Streamlit's session recreation on redirect.
    """
    flow  = _make_flow()
    state = _make_signed_state()

    auth_url, _ = flow.authorization_url(
        access_type="offline",
        prompt="select_account",
        state=state,
        include_granted_scopes="true",
    )
    return auth_url


def handle_oauth_callback(code: str, returned_state: str) -> None:
    """Exchange authorization code for tokens after verifying signed state.

    Raises ValueError if the state signature is invalid (CSRF protection).
    Tokens stored only in st.session_state (Fix M-3, Fix C-1).
    """
    if not _verify_signed_state(returned_state):
        raise ValueError(
            "OAuth state verification failed — possible CSRF attempt. "
            "Please start the sign-in flow again."
        )

    flow = _make_flow()

    # Pass the state through so requests_oauthlib doesn't re-validate it
    # with a session it doesn't have
    flow.fetch_token(code=code, state=returned_state)
    creds = flow.credentials

    st.session_state["google_creds"] = {
        "token":         creds.token,
        "refresh_token": creds.refresh_token,
        "token_uri":     creds.token_uri,
        "client_id":     creds.client_id,
        "client_secret": creds.client_secret,
        "scopes":        list(creds.scopes or GMAIL_SCOPES),
    }
    st.session_state["authenticated"]   = True
    st.session_state["connected_email"] = "Gmail account"


def get_credentials() -> Credentials:
    """Reconstruct a fresh Credentials object from session state (Fix C-1).
    Auto-refreshes if expired (Fix M-4).
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
    """Permanently wipe all auth state — called after every scan."""
    for key in ("google_creds", "authenticated", "connected_email", "oauth_state"):
        st.session_state.pop(key, None)


def is_authenticated() -> bool:
    return bool(
        st.session_state.get("authenticated")
        and st.session_state.get("google_creds")
    )

"""
Google OAuth 2.0 authentication for Streamlit.

Two challenges solved here:

1. Streamlit session loss on redirect
   When the user leaves to Google and returns, Streamlit creates a NEW session.
   Any data stored in st.session_state before the redirect is gone.
   Fix: HMAC-signed state tokens — self-verifying, no session storage needed.

2. PKCE code_verifier lost across sessions
   google-auth-oauthlib generates a PKCE code_verifier when building the
   auth URL. On callback we create a fresh Flow — it has no code_verifier.
   Google rejects the token exchange with "Missing code verifier."
   Fix: We generate our own PKCE pair and embed the code_verifier inside
   the signed state token. It travels with the OAuth redirect and is
   extracted on callback — no session dependency.

State token format:
    base64url(code_verifier) + "." + base64url(HMAC-SHA256(code_verifier))
    The code_verifier is 32 bytes of cryptographic randomness — it doubles
    as both the CSRF nonce and the PKCE verifier.

Other security design:
  - FIX C-1: No shared OAuth singleton. Fresh Flow/Credentials per operation.
  - FIX H-3: Secrets fail fast if missing — no silent fallback defaults.
  - FIX M-3: Tokens live only in st.session_state (per-user RAM, never disk).
  - FIX M-4: access_type='offline' so refresh tokens are issued.
  - Privacy: logout() wipes all token state after every scan.
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


# ── PKCE helpers ──────────────────────────────────────────────────────────────

def _generate_pkce() -> tuple[str, str]:
    """Generate a PKCE code_verifier and its S256 code_challenge.

    code_verifier: 32 bytes of cryptographic randomness, base64url-encoded.
    code_challenge: SHA-256(code_verifier), base64url-encoded (no padding).
    """
    raw = secrets.token_bytes(32)
    code_verifier  = base64.urlsafe_b64encode(raw).decode().rstrip("=")
    digest         = hashlib.sha256(code_verifier.encode()).digest()
    code_challenge = base64.urlsafe_b64encode(digest).decode().rstrip("=")
    return code_verifier, code_challenge


# ── Signed state tokens ───────────────────────────────────────────────────────

def _make_signed_state(code_verifier: str) -> str:
    """Sign the code_verifier with HMAC-SHA256 to create the OAuth state token.

    Format: base64url(code_verifier) + "." + base64url(HMAC)
    The code_verifier is already base64url-encoded, so we sign it directly.
    The GOOGLE_CLIENT_SECRET is the signing key — stable and secret.
    """
    key = _get_secret("GOOGLE_CLIENT_SECRET").encode()
    sig = hmac.new(key, code_verifier.encode(), hashlib.sha256).digest()
    sig_b64 = base64.urlsafe_b64encode(sig).decode().rstrip("=")
    return f"{code_verifier}.{sig_b64}"


def _verify_and_extract(state: str) -> str | None:
    """Verify the signed state token and return the code_verifier.

    Returns None if the signature is invalid — caller should raise ValueError.
    Uses constant-time comparison to prevent timing attacks.
    """
    try:
        cv, received_sig = state.rsplit(".", 1)
        key = _get_secret("GOOGLE_CLIENT_SECRET").encode()
        expected_sig = hmac.new(key, cv.encode(), hashlib.sha256).digest()
        expected_b64 = base64.urlsafe_b64encode(expected_sig).decode().rstrip("=")
        if hmac.compare_digest(received_sig, expected_b64):
            return cv
    except Exception:
        pass
    return None


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
    """Build the Google sign-in URL with self-contained PKCE + CSRF state.

    We generate the PKCE pair ourselves so the code_verifier can be embedded
    in the signed state token and recovered on callback without session storage.
    """
    code_verifier, code_challenge = _generate_pkce()
    state = _make_signed_state(code_verifier)

    flow = _make_flow()
    auth_url, _ = flow.authorization_url(
        access_type="offline",
        prompt="select_account",
        state=state,
        code_challenge=code_challenge,
        code_challenge_method="S256",
        include_granted_scopes="true",
    )
    return auth_url


def handle_oauth_callback(code: str, returned_state: str) -> None:
    """Verify signed state, recover code_verifier, exchange code for tokens."""
    code_verifier = _verify_and_extract(returned_state)
    if not code_verifier:
        raise ValueError(
            "OAuth state verification failed — possible CSRF attempt. "
            "Please start the sign-in flow again."
        )

    flow = _make_flow()
    flow.fetch_token(
        code=code,
        code_verifier=code_verifier,   # Required — completes the PKCE exchange
    )
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
    """Reconstruct a fresh Credentials object from session state (Fix C-1)."""
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

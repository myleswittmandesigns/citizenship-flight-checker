"""
IMAP authentication and connection management.

Security design:
  - SSL/TLS enforced on every connection (port 993) — plaintext never attempted.
  - App password (not main password) is the expected credential.
  - Credentials stored only in st.session_state (per-user RAM, not disk).
  - IMAP connection is opened, used, and closed per-scan — not kept alive.
  - Connection timeout prevents hanging on bad server responses.
  - Credentials cleared on logout.
"""

import imaplib
import socket
import streamlit as st

# ── Provider presets ──────────────────────────────────────────────────────────
# Each entry: IMAP host, port, folders to search, and app-password guidance.
PROVIDERS: dict[str, dict] = {
    "Gmail": {
        "host": "imap.gmail.com",
        "port": 993,
        "folders": ["INBOX", "[Gmail]/Sent Mail"],
        "app_password_url": "https://myaccount.google.com/apppasswords",
        "app_password_steps": (
            "1. Go to your Google Account → **Security**\n"
            "2. Under 'How you sign in to Google', select **2-Step Verification** "
            "(must be enabled first)\n"
            "3. At the bottom of that page, select **App passwords**\n"
            "4. Choose app: *Mail* — Choose device: *Other* — click **Generate**\n"
            "5. Copy the 16-character password shown"
        ),
    },
    "Outlook / Hotmail / Live": {
        "host": "outlook.office365.com",
        "port": 993,
        "folders": ["INBOX", "Sent"],
        "app_password_url": "https://account.microsoft.com/security",
        "app_password_steps": (
            "1. Go to **account.microsoft.com/security**\n"
            "2. Select **Advanced security options**\n"
            "3. Under 'App passwords', select **Create a new app password**\n"
            "4. Copy the generated password\n\n"
            "*(Requires Microsoft 2-step verification to be enabled)*"
        ),
    },
    "Yahoo Mail": {
        "host": "imap.mail.yahoo.com",
        "port": 993,
        "folders": ["INBOX", "Sent"],
        "app_password_url": "https://myaccount.yahoo.com/security",
        "app_password_steps": (
            "1. Go to **myaccount.yahoo.com/security**\n"
            "2. Select **Generate app password**\n"
            "3. Choose *Other app* from the dropdown\n"
            "4. Enter a name (e.g. 'FlightChecker') and click **Generate**\n"
            "5. Copy the password shown"
        ),
    },
    "iCloud / Apple Mail": {
        "host": "imap.mail.me.com",
        "port": 993,
        "folders": ["INBOX", "Sent Messages"],
        "app_password_url": "https://appleid.apple.com",
        "app_password_steps": (
            "1. Go to **appleid.apple.com** and sign in\n"
            "2. Select **Sign-In and Security → App-Specific Passwords**\n"
            "3. Click **Generate an app-specific password**\n"
            "4. Enter a label (e.g. 'FlightChecker') and click **Create**\n"
            "5. Copy the password shown\n\n"
            "*(Requires Apple ID two-factor authentication to be enabled)*"
        ),
    },
    "Other (custom IMAP)": {
        "host": "",
        "port": 993,
        "folders": ["INBOX"],
        "app_password_url": None,
        "app_password_steps": (
            "Check your email provider's help pages for:\n"
            "- IMAP server address\n"
            "- Whether they support app passwords or require your regular password"
        ),
    },
}


def connect(host: str, port: int, email_address: str, app_password: str) -> imaplib.IMAP4_SSL:
    """Open an SSL IMAP connection and authenticate. Returns the connection object.

    Raises:
        imaplib.IMAP4.error  — bad credentials
        socket.timeout       — server unreachable / timed out
        ConnectionRefusedError — port blocked or wrong host
        OSError              — network-level failure
    """
    # 30-second socket timeout prevents indefinite hangs
    imap = imaplib.IMAP4_SSL(host=host, port=port)
    imap.socket().settimeout(30)
    imap.login(email_address, app_password)
    return imap


def test_connection(host: str, port: int, email_address: str, app_password: str) -> tuple[bool, str]:
    """Attempt a connection and return (success, message).

    Used by the UI to validate credentials before starting a full scan.
    Connection is closed immediately after the test.
    """
    try:
        imap = connect(host, port, email_address, app_password)
        imap.logout()
        return True, "Connected successfully."
    except imaplib.IMAP4.error as exc:
        msg = str(exc).lower()
        if "invalid credentials" in msg or "authentication failed" in msg:
            return False, (
                "Incorrect email or app password. "
                "Make sure you're using an **app password**, not your regular login password."
            )
        return False, f"IMAP error: {exc}"
    except (socket.timeout, TimeoutError):
        return False, "Connection timed out. Check that IMAP is enabled for your account."
    except ConnectionRefusedError:
        return False, f"Connection refused on {host}:{port}. Verify the server settings."
    except OSError as exc:
        return False, f"Network error: {exc}"


def save_credentials(email_address: str, app_password: str, host: str, port: int, folders: list[str]) -> None:
    """Store IMAP credentials in session state only — never written to disk."""
    st.session_state["imap_creds"] = {
        "email":    email_address,
        "password": app_password,   # app password, not main password
        "host":     host,
        "port":     port,
        "folders":  folders,
    }
    st.session_state["authenticated"] = True
    st.session_state["connected_email"] = email_address


def get_credentials() -> dict:
    """Retrieve stored IMAP credentials from session state."""
    creds = st.session_state.get("imap_creds")
    if not creds:
        raise RuntimeError("Not authenticated. Please connect your email first.")
    return creds


def logout() -> None:
    """Clear all auth-related state."""
    for key in ("imap_creds", "authenticated", "connected_email"):
        st.session_state.pop(key, None)


def is_authenticated() -> bool:
    return bool(st.session_state.get("authenticated") and st.session_state.get("imap_creds"))

"""Per-account OAuth handling.

Each Google account is connected once via the standard OAuth desktop flow.
Refresh tokens are stored in the OS keyring (never in plain files); only
non-secret metadata (alias -> email) lives in accounts.json.
"""

from __future__ import annotations

import json

import keyring
from google.auth.exceptions import RefreshError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow

from .config import (
    ACCOUNTS_FILE,
    ALL_SCOPES,
    CLIENT_SECRET_FILE,
    KEYRING_SERVICE,
    ensure_dirs,
)


class NeedsReconnect(RuntimeError):
    """The stored token is missing, expired, or revoked.

    Raised instead of a generic error so callers (CLI, web UI) can surface a
    "reconnect needed" state rather than a cryptic failure. Subclasses
    RuntimeError so the CLI's top-level error handler prints it cleanly.
    """


def _token_username(alias: str) -> str:
    return f"token:{alias}"


def load_accounts() -> dict:
    ensure_dirs()
    if not ACCOUNTS_FILE.exists():
        return {}
    return json.loads(ACCOUNTS_FILE.read_text())


def save_accounts(accounts: dict) -> None:
    ensure_dirs()
    ACCOUNTS_FILE.write_text(json.dumps(accounts, indent=2))
    ACCOUNTS_FILE.chmod(0o600)


def _run_oauth_flow() -> tuple:
    """Run the Google sign-in flow; return (credentials, email address)."""
    flow = InstalledAppFlow.from_client_secrets_file(
        str(CLIENT_SECRET_FILE), scopes=ALL_SCOPES
    )
    creds = flow.run_local_server(port=0, prompt="consent")

    # Resolve the account's email address so the alias maps to something human.
    from googleapiclient.discovery import build

    service = build("drive", "v3", credentials=creds)
    email = (
        service.about()
        .get(fields="user(emailAddress)")
        .execute()["user"]["emailAddress"]
    )
    return creds, email


def add_account(alias: str) -> str:
    """Run the OAuth flow for a new account and store its token. Returns email."""
    if not CLIENT_SECRET_FILE.exists():
        raise FileNotFoundError(
            f"Missing {CLIENT_SECRET_FILE}.\n"
            "Create a Google Cloud project, enable the Google Drive API, create an "
            "'OAuth client ID' of type Desktop, and save the downloaded JSON as "
            f"{CLIENT_SECRET_FILE}. See README.md for step-by-step instructions."
        )
    accounts = load_accounts()
    if alias in accounts:
        raise ValueError(f"An account with alias '{alias}' already exists.")

    creds, email = _run_oauth_flow()
    keyring.set_password(KEYRING_SERVICE, _token_username(alias), creds.refresh_token)
    accounts[alias] = {"email": email}
    save_accounts(accounts)
    return email


def reconnect_account(alias: str) -> str:
    """Re-run the OAuth flow for an existing account, replacing its token.

    Used when the stored token expired or was revoked. The ledger, scans,
    and all other accounts are untouched — only this alias's keyring entry
    (and email, in case it changed) is updated. Returns the email address.
    """
    accounts = load_accounts()
    if alias not in accounts:
        raise ValueError(f"No account with alias '{alias}'.")
    creds, email = _run_oauth_flow()
    keyring.set_password(KEYRING_SERVICE, _token_username(alias), creds.refresh_token)
    accounts[alias]["email"] = email
    save_accounts(accounts)
    return email


def check_account(alias: str) -> dict:
    """Check whether an account's credentials work. Never raises.

    Returns {"ok", "needs_reconnect", "error"}.
    """
    try:
        get_credentials(alias)
        return {"ok": True, "needs_reconnect": False, "error": ""}
    except NeedsReconnect as exc:
        return {"ok": False, "needs_reconnect": True, "error": str(exc)[:200]}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "needs_reconnect": False, "error": str(exc)[:200]}


def remove_account(alias: str) -> None:
    accounts = load_accounts()
    if alias not in accounts:
        raise ValueError(f"No account with alias '{alias}'.")
    try:
        keyring.delete_password(KEYRING_SERVICE, _token_username(alias))
    except Exception:
        pass
    del accounts[alias]
    save_accounts(accounts)


def get_credentials(alias: str) -> Credentials:
    """Return valid credentials for alias, refreshing silently if needed.

    Raises NeedsReconnect when the stored token is missing, expired, or
    revoked — the caller should offer the reconnect flow, not a traceback.
    """
    accounts = load_accounts()
    if alias not in accounts:
        raise ValueError(
            f"No account with alias '{alias}'. Run: spillover auth add --alias {alias}"
        )
    refresh_token = keyring.get_password(KEYRING_SERVICE, _token_username(alias))
    if not refresh_token:
        raise NeedsReconnect(
            f"No stored token for '{alias}'. "
            f"Reconnect with: spillover auth reconnect --alias {alias}"
        )
    info = json.loads(CLIENT_SECRET_FILE.read_text())
    installed = info.get("installed", info)
    creds = Credentials(
        token=None,
        refresh_token=refresh_token,
        token_uri=installed["token_uri"],
        client_id=installed["client_id"],
        client_secret=installed["client_secret"],
        scopes=ALL_SCOPES,
    )
    if not creds.valid:
        try:
            creds.refresh(Request())  # may raise if revoked -> user re-auths
        except RefreshError as exc:
            raise NeedsReconnect(
                f"Google rejected the stored token for '{alias}' "
                "(expired or revoked). "
                f"Reconnect with: spillover auth reconnect --alias {alias}"
            ) from exc
    return creds

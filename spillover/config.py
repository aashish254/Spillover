"""Shared constants and paths. Everything lives under ~/.spillover on the user's PC."""

from pathlib import Path

APP_NAME = "spillover"

DATA_DIR = Path.home() / ".spillover"
ACCOUNTS_FILE = DATA_DIR / "accounts.json"
CLIENT_SECRET_FILE = DATA_DIR / "client_secret.json"
LEDGER_DB = DATA_DIR / "ledger.db"
SCANS_DIR = DATA_DIR / "scans"
PLANS_DIR = DATA_DIR / "plans"
TMP_DIR = DATA_DIR / "tmp"
PHOTO_MANIFESTS_DIR = DATA_DIR / "photo_manifests"

# OAuth scopes. Deliberately narrow: Drive plus the Photos *Picker* scope.
# The picker scope only ever exposes items the user explicitly selects in
# Google's own picker UI — never the whole photo library. Spillover never
# asks for Gmail or Photos-library scopes, so it can never see your mail
# or browse your photos uninvited.
DRIVE_SCOPES = ["https://www.googleapis.com/auth/drive"]
PHOTOS_PICKER_SCOPES = [
    "https://www.googleapis.com/auth/photospicker.mediaitems.readonly"
]
ALL_SCOPES = DRIVE_SCOPES + PHOTOS_PICKER_SCOPES

# Folder created on every destination account to hold moved files.
DEST_FOLDER_NAME = "Spillover"

# Never fill a destination account past this much free space left over.
SAFETY_BUFFER_BYTES = 1 * 1024**3  # 1 GiB

# Resumable upload chunk size.
UPLOAD_CHUNK_BYTES = 8 * 1024 * 1024  # 8 MiB

# Retry policy for transient Google API errors.
MAX_RETRIES = 5
RETRYABLE_STATUS = {403, 429, 500, 502, 503}

# keyring service name for OAuth tokens.
KEYRING_SERVICE = "spillover"


def ensure_dirs() -> None:
    for d in (DATA_DIR, SCANS_DIR, PLANS_DIR, TMP_DIR, PHOTO_MANIFESTS_DIR):
        d.mkdir(parents=True, exist_ok=True)

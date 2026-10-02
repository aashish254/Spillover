"""Scan an account's Drive and summarize what's eating the quota."""

from __future__ import annotations

import json
import time
from collections import Counter
from pathlib import Path

from tqdm import tqdm

from .config import SCANS_DIR, ensure_dirs
from .drive import DriveClient

# Native Google Workspace files consume no quota; flag them so the user
# doesn't waste time "moving" files that free zero bytes.
NATIVE_MIME_PREFIX = "application/vnd.google-apps."


def is_quota_relevant(f: dict) -> bool:
    return not f["mimeType"].startswith(NATIVE_MIME_PREFIX) and f["size"] > 0


def scan(alias: str) -> dict:
    """Full Drive inventory for alias. Returns summary dict; saves detail JSON."""
    ensure_dirs()
    client = DriveClient(alias)
    files = []
    for f in tqdm(
        client.iter_files(), desc=f"Scanning {alias}", unit="files", leave=False
    ):
        # Sharing metadata for the move-safety checks (see sharing.py).
        # The verbose raw API blobs are reduced to derived fields.
        owners = f.get("owners") or []
        files.append(
            {
                "id": f["id"],
                "name": f["name"],
                "mimeType": f["mimeType"],
                "size": f["size"],
                "modifiedTime": f.get("modifiedTime"),
                "sha256": f.get("sha256Checksum"),
                "shared": bool(f.get("shared")),
                "owner_emails": [
                    o.get("emailAddress") for o in owners
                    if o.get("emailAddress")
                ],
                "owned_by_me": (
                    any(o.get("me") for o in owners) if owners else True
                ),
            }
        )

    quota_files = [f for f in files if is_quota_relevant(f)]
    by_ext: Counter = Counter()
    for f in quota_files:
        ext = Path(f["name"]).suffix.lower() or "(no ext)"
        by_ext[ext] += f["size"]

    summary = {
        "alias": alias,
        "scanned_at": int(time.time()),
        "total_files": len(files),
        "quota_files": len(quota_files),
        "quota_bytes": sum(f["size"] for f in quota_files),
        "top_extensions": by_ext.most_common(10),
        "largest": sorted(quota_files, key=lambda f: f["size"], reverse=True)[:20],
    }
    out = SCANS_DIR / f"{alias}.json"
    out.write_text(json.dumps({"summary": summary, "files": quota_files}, indent=1))
    return summary


def load_scan(alias: str) -> dict:
    path = SCANS_DIR / f"{alias}.json"
    if not path.exists():
        raise FileNotFoundError(
            f"No scan found for '{alias}'. Run: spillover scan --alias {alias}"
        )
    return json.loads(path.read_text())

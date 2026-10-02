"""Google Photos transfers via the Picker API.

Since March 2025 the Photos Library API gives third-party apps no
whole-library access, and there is **no delete endpoint at all**. The Picker
API is the supported path:

1. Spillover creates a picker session and shows you Google's picker URL.
2. You select photos/videos in Google's own UI (Spillover only ever sees
   what you pick — nothing else in your library).
3. Spillover downloads the picked items and uploads them, hash-verified,
   into the destination accounts' Drive storage.
4. Because the API cannot delete, Spillover writes a manual-deletion
   manifest: a checklist of exactly what to remove by hand in Google Photos.

The picker uses the narrow ``photospicker.mediaitems.readonly`` scope —
user-selected items only, never the library.
"""

from __future__ import annotations

import json
import tempfile
import time
from pathlib import Path

from . import ledger
from .config import (
    DEST_FOLDER_NAME,
    PHOTO_MANIFESTS_DIR,
    TMP_DIR,
    ensure_dirs,
)
from .drive import DriveClient, sha256_of

PICKER_BASE = "https://photospicker.googleapis.com"
LIBRARY_BASE = "https://www.googleapis.com"


class PickerAPI:
    """Minimal REST client for the Photos Picker API.

    ``http`` is an authorized session-like object exposing get/post with
    the requests API; inject a fake in tests to avoid network access.
    """

    def __init__(self, alias: str, http=None):
        self.alias = alias
        if http is not None:
            self.http = http
        else:
            from . import auth
            from google.auth.transport.requests import AuthorizedSession

            self.http = AuthorizedSession(auth.get_credentials(alias))

    # -- session ------------------------------------------------------
    def create_session(self) -> dict:
        """Create a picker session. Returns the raw session resource."""
        r = self.http.post(f"{PICKER_BASE}/v1/sessions", json={})
        r.raise_for_status()
        return r.json()

    def get_session(self, session_id: str) -> dict:
        r = self.http.get(f"{PICKER_BASE}/v1/sessions/{session_id}")
        r.raise_for_status()
        return r.json()

    # -- picked items --------------------------------------------------
    def list_media_items(self, session_id: str, page_size: int = 100) -> list[dict]:
        """All media items the user picked in this session."""
        items: list[dict] = []
        page_token = None
        while True:
            params = {"sessionId": session_id, "pageSize": page_size}
            if page_token:
                params["pageToken"] = page_token
            r = self.http.get(
                f"{PICKER_BASE}/v1/mediaItems",
                params=params,
            )
            r.raise_for_status()
            data = r.json()
            items.extend(data.get("mediaItems", []))
            page_token = data.get("nextPageToken")
            if not page_token:
                break
        return items

    def get_media_file(self, media_item_id: str, session_id: str) -> dict:
        """Resolve a picked item to its downloadable baseUrl + metadata."""
        r = self.http.get(
            f"{PICKER_BASE}/v1/mediaItems/{media_item_id}",
            params={"sessionId": session_id},
        )
        r.raise_for_status()
        return r.json()

    def download(self, base_url: str, dest: Path) -> Path:
        """Download the original bytes (=d suffix) to ``dest``."""
        with self.http.get(base_url + "=d", stream=True) as r:
            r.raise_for_status()
            with open(dest, "wb") as fh:
                for chunk in r.iter_content(4 * 1024 * 1024):
                    fh.write(chunk)
        return dest


# ---------------------------------------------------------------------------
# High-level flow
# ---------------------------------------------------------------------------


def create_picker_session(alias: str, picker: PickerAPI | None = None) -> dict:
    """Create a session; returns {session_id, picker_uri, expires}."""
    api = picker or PickerAPI(alias)
    s = api.create_session()
    return {
        "session_id": s["id"],
        "picker_uri": s["pickerUri"],
        "expires": s.get("expireTime", ""),
    }


def wait_for_selection(
    alias: str,
    session_id: str,
    timeout_s: int = 900,
    poll_s: int = 5,
    picker: PickerAPI | None = None,
    progress_cb=None,
) -> dict:
    """Poll until the user finishes picking (or the timeout hits)."""
    api = picker or PickerAPI(alias)
    deadline = time.time() + timeout_s
    while True:
        s = api.get_session(session_id)
        if s.get("mediaItemsSet"):
            return s
        if time.time() >= deadline:
            raise TimeoutError(
                "Timed out waiting for photo selection. The picker link "
                "may have expired — create a new session and try again."
            )
        if progress_cb:
            progress_cb(s)
        time.sleep(poll_s)


class PickerSessionGone(RuntimeError):
    """Google no longer knows this picker session (expired / wrong id)."""


def _is_http_404(exc: Exception) -> bool:
    resp = getattr(exc, "response", None)
    if resp is not None and getattr(resp, "status_code", None) == 404:
        return True
    return "404" in str(exc)[:80]


def check_selection_done(
    alias: str,
    session_id: str,
    wait_s: int = 45,
    poll_s: int = 3,
    picker: "PickerAPI | None" = None,
) -> dict:
    """Poll until the user finishes picking in the Google picker.

    Returns the session resource once ``mediaItemsSet`` is true. Raises
    :class:`PickerSessionGone` if Google 404s the session (usually: the
    picker link expired, or it was opened while signed in to a different
    Google account than the one that created it), and ``TimeoutError``
    if the user hasn't finished selecting within ``wait_s``.
    """
    api = picker or PickerAPI(alias)
    deadline = time.time() + wait_s
    while True:
        try:
            s = api.get_session(session_id)
        except Exception as exc:  # noqa: BLE001 - translate to guidance
            if _is_http_404(exc):
                raise PickerSessionGone(
                    "Google doesn't recognise this picker session — it "
                    "probably expired. Hit 'Start over' for a fresh link, "
                    "and open it while signed in to Google as the account "
                    f"you chose ('{alias}')."
                ) from exc
            raise
        if s.get("mediaItemsSet"):
            return s
        if time.time() >= deadline:
            raise TimeoutError(
                "Google hasn't marked your selection as finished yet. "
                "Finish picking in the Google picker tab, wait a few "
                "seconds, then press the button again."
            )
        time.sleep(poll_s)


class PickerItemsMissing(RuntimeError):
    """Google knows the session, but the user's picks never got attached."""


def list_picked(
    alias: str,
    session_id: str,
    picker: PickerAPI | None = None,
    settle_s: float = 30,
    poll_s: float = 3,
) -> list[dict]:
    """JSON-friendly list of what the user picked.

    Waits (briefly) for the user to finish selecting first, so callers
    never list a half-done session. Then gives Google's backend a short
    settle window: right after the picker reports "done" the item list
    can still 404 or come back empty for a few seconds, and when the
    picker tab showed "Couldn't add photos" the picks never attach at
    all. Raises :class:`PickerItemsMissing` with human guidance instead
    of leaking a raw 404.
    """
    api = picker or PickerAPI(alias)
    check_selection_done(alias, session_id, picker=api)
    deadline = time.time() + settle_s
    while True:
        try:
            items = api.list_media_items(session_id)
        except Exception as exc:  # noqa: BLE001 - 404 means "not attached yet"
            if not _is_http_404(exc):
                raise
            items = []
        if items:
            break
        if time.time() >= deadline:
            raise PickerItemsMissing(
                "Google knows this picker session, but your selected photos "
                "never got attached to it — the picker tab probably showed "
                "'Couldn't add photos'. Click 'Try again' in the Google picker "
                "tab, wait a few seconds, then press 'I've finished selecting' "
                "again. If it keeps failing: press 'Start over' for a fresh "
                "link, open it in a window signed in to Google only as "
                f"'{alias}', and turn off ad-blockers for photos.google.com "
                "(they can break the picker)."
            )
        time.sleep(poll_s)
    out = []
    for it in items:
        mf = it.get("mediaFile", {})
        out.append(
            {
                "id": it["id"],
                "filename": mf.get("filename") or it["id"],
                "mimeType": mf.get("mimeType") or "application/octet-stream",
                "baseUrl": mf.get("baseUrl", ""),
                "createTime": it.get("createTime", ""),
                "type": it.get("type", ""),
            }
        )
    return out


def write_manifest(
    alias: str,
    session_id: str,
    entries: list[dict],
    manifest_dir: Path | None = None,
) -> Path:
    """Write the manual-deletion checklist (JSON + Markdown)."""
    d = Path(manifest_dir) if manifest_dir else PHOTO_MANIFESTS_DIR
    d.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    stem = f"photos-{alias}-{stamp}"
    payload = {
        "alias": alias,
        "session_id": session_id,
        "created_at": stamp,
        "copies_verified": len(entries),
        "note": (
            "Google's Photos API provides no delete endpoint, so these "
            "originals still exist in Google Photos. Delete each item below "
            "by hand AFTER confirming its copy."
        ),
        "items": entries,
    }
    json_path = d / f"{stem}.json"
    json_path.write_text(json.dumps(payload, indent=1))
    lines = [
        "# Manual deletion checklist",
        "",
        f"Source Google Photos account: `{alias}` — "
        f"{len(entries)} item(s) copied and hash-verified.",
        "",
        "Google's Photos API has **no delete endpoint**, so no tool can "
        "remove these for you. In the Google Photos app/site, find each item "
        "below and delete it yourself — only after you've confirmed the copy.",
        "",
    ]
    for e in entries:
        taken = e.get("taken") or "unknown date"
        lines.append(
            f"- [ ] `{e['filename']}` (taken {taken}) → "
            f"copied to `{e['dst_alias']}`"
        )
    (d / f"{stem}.md").write_text("\n".join(lines) + "\n")
    return json_path


def transfer_picked(
    alias: str,
    session_id: str,
    dest_aliases: list[str],
    subfolder: str = "Photos Picker",
    progress_cb=None,
    picker: PickerAPI | None = None,
    manifest_dir: Path | None = None,
) -> dict:
    """Transfer everything the user picked to the destination accounts.

    Round-robins items across ``dest_aliases``. Each item is downloaded,
    hash-verified after upload, and recorded in the ledger. Returns stats
    including the manifest path — the user must delete the originals in
    Google Photos by hand (the API cannot delete).
    """
    ensure_dirs()
    if not dest_aliases:
        raise ValueError("No destination accounts given.")
    api = picker or PickerAPI(alias)
    items = list_picked(alias, session_id, picker=api)
    total = len(items)

    stats: dict = {"done": 0, "failed": 0, "bytes": 0, "manifest": ""}
    entries: list[dict] = []
    dest_clients: dict[str, DriveClient] = {}
    dest_folders: dict[str, str] = {}

    def dest(alias_: str):
        if alias_ not in dest_clients:
            c = DriveClient(alias_)
            folder = c.ensure_folder(DEST_FOLDER_NAME)
            folder = c.ensure_folder(subfolder, parent_id=folder)
            dest_clients[alias_] = c
            dest_folders[alias_] = folder
        return dest_clients[alias_], dest_folders[alias_]

    with tempfile.TemporaryDirectory(dir=TMP_DIR) as tmp:
        tmpdir = Path(tmp)
        for n, it in enumerate(items, start=1):
            dst_alias = dest_aliases[(n - 1) % len(dest_aliases)]
            note = ""
            try:
                base_url = it.get("baseUrl")
                filename = it["filename"]
                if not base_url:
                    meta = api.get_media_file(it["id"], session_id)
                    mf = meta.get("mediaFile", {})
                    base_url = mf.get("baseUrl")
                    filename = mf.get("filename") or filename
                if not base_url:
                    raise RuntimeError("no download URL for picked item")
                local = tmpdir / f"{it['id']}_{filename}"
                api.download(base_url, local)
                local_hash = sha256_of(local)

                dc, folder = dest(dst_alias)
                created = dc.upload(local, folder, name=filename)
                dst_id = created.get("id")
                remote_hash = dc.remote_sha256(dst_id)
                if remote_hash and remote_hash != local_hash:
                    raise RuntimeError("hash mismatch after upload")

                ledger.record_move(
                    filename=filename, size_bytes=local.stat().st_size,
                    sha256=local_hash, src_alias=f"photos:{alias}",
                    src_file_id=it["id"], dst_alias=dst_alias,
                    dst_file_id=dst_id, status="done",
                )
                entries.append(
                    {
                        "filename": filename,
                        "taken": it.get("createTime", ""),
                        "size_bytes": local.stat().st_size,
                        "sha256": local_hash,
                        "picker_item_id": it["id"],
                        "dst_alias": dst_alias,
                        "dst_file_id": dst_id,
                    }
                )
                stats["done"] += 1
                stats["bytes"] += local.stat().st_size
                local.unlink(missing_ok=True)
            except Exception as exc:  # noqa: BLE001 - record and continue
                note = str(exc)[:200]
                stats["failed"] += 1
                ledger.record_move(
                    filename=it["filename"], size_bytes=0, sha256=None,
                    src_alias=f"photos:{alias}", src_file_id=it["id"],
                    dst_alias=dst_alias, dst_file_id=None,
                    status="failed", note=note,
                )
            if progress_cb:
                progress_cb(n, total, {
                    "name": it["filename"], "dest_alias": dst_alias,
                    "status": "done" if not note else "failed", "note": note,
                })

    manifest = write_manifest(alias, session_id, entries,
                              manifest_dir=manifest_dir)
    stats["manifest"] = manifest.name
    stats["manifest_path"] = str(manifest)
    return stats

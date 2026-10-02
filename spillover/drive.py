"""Thin, retry-hardened wrapper around the Google Drive v3 API."""

from __future__ import annotations

import hashlib
import mimetypes
import time
from pathlib import Path
from typing import Iterator, Optional

from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaFileUpload, MediaIoBaseDownload

from . import auth
from .config import MAX_RETRIES, RETRYABLE_STATUS, UPLOAD_CHUNK_BYTES


def _is_retryable(exc: HttpError) -> bool:
    if exc.resp is None:
        return True
    status = exc.resp.status
    if status not in RETRYABLE_STATUS:
        return False
    # 403 is only retryable for rate-limit reasons, not permission errors.
    if status == 403:
        try:
            reason = exc.error_details[0].get("reason", "") if exc.error_details else ""
        except Exception:
            reason = ""
        return "rateLimit" in reason.lower() or "userRateLimit" in reason.lower()
    return True


def with_retries(fn, *args, **kwargs):
    """Call a Google API request function with truncated exponential backoff."""
    delay = 1.0
    for attempt in range(MAX_RETRIES):
        try:
            return fn(*args, **kwargs)
        except HttpError as exc:
            if attempt == MAX_RETRIES - 1 or not _is_retryable(exc):
                raise
            time.sleep(delay)
            delay = min(delay * 2, 64)


class DriveClient:
    def __init__(self, alias: str):
        self.alias = alias
        self.service = build("drive", "v3", credentials=auth.get_credentials(alias))

    # -- quota -----------------------------------------------------------
    def quota(self) -> dict:
        res = with_retries(
            self.service.about()
            .get(fields="storageQuota(limit,usage,usageInDrive,usageInDriveTrash)")
            .execute
        )
        q = res.get("storageQuota", {})
        limit = int(q.get("limit") or 0)
        usage = int(q.get("usage") or 0)
        return {"limit": limit, "usage": usage, "free": max(limit - usage, 0)}

    # -- listing ---------------------------------------------------------
    def iter_files(self, page_size: int = 1000) -> Iterator[dict]:
        """Yield every non-trashed file the account can see, with sizes.

        Sharing metadata (``shared`` flag, ``owners[]``, ``capabilities``)
        is included so the planner can warn about — and skip by default —
        files that other people can access or that this account doesn't own.
        """
        page_token: Optional[str] = None
        while True:
            res = with_retries(
                self.service.files()
                .list(
                    q="trashed = false",
                    fields=(
                        "nextPageToken, files(id,name,mimeType,size,parents,"
                        "sha256Checksum,modifiedTime,shared,"
                        "owners(emailAddress,me),capabilities(canDelete))"
                    ),
                    pageSize=page_size,
                    pageToken=page_token,
                )
                .execute
            )
            for f in res.get("files", []):
                f["size"] = int(f.get("size") or 0)  # native Docs have no size
                yield f
            page_token = res.get("nextPageToken")
            if not page_token:
                break

    # -- download --------------------------------------------------------
    def download(self, file_id: str, dest: Path, progress_cb=None) -> Path:
        request = self.service.files().get_media(fileId=file_id)

        def _do():
            with open(dest, "wb") as fh:
                downloader = MediaIoBaseDownload(fh, request)
                done = False
                while not done:
                    status, done = downloader.next_chunk()
                    if progress_cb and status:
                        progress_cb(status.resumable_progress)

        with_retries(_do)
        return dest

    # -- upload ----------------------------------------------------------
    def ensure_folder(self, name: str, parent_id: str = "root") -> str:
        """Return the id of a folder called `name` under parent, creating it."""
        q = (
            f"name = '{name}' and mimeType = 'application/vnd.google-apps.folder' "
            f"and '{parent_id}' in parents and trashed = false"
        )
        res = with_retries(
            self.service.files()
            .list(q=q, fields="files(id)", pageSize=1)
            .execute
        )
        files = res.get("files", [])
        if files:
            return files[0]["id"]
        body = {
            "name": name,
            "mimeType": "application/vnd.google-apps.folder",
            "parents": [parent_id],
        }
        created = with_retries(
            self.service.files().create(body=body, fields="id").execute
        )
        return created["id"]

    def upload(self, local_path: Path, folder_id: str, name: Optional[str] = None) -> dict:
        """Resumable upload. Returns the created file resource."""
        name = name or local_path.name
        mime, _ = mimetypes.guess_type(str(local_path))
        media = MediaFileUpload(
            str(local_path),
            mimetype=mime or "application/octet-stream",
            resumable=True,
            chunksize=UPLOAD_CHUNK_BYTES,
        )
        body = {"name": name, "parents": [folder_id]}
        request = self.service.files().create(
            body=body, media_body=media, fields="id,name,size,sha256Checksum"
        )
        response = None
        while response is None:
            try:
                _, response = request.next_chunk()
            except HttpError as exc:
                if not _is_retryable(exc):
                    raise
                time.sleep(2)
        return response

    def remote_sha256(self, file_id: str) -> Optional[str]:
        res = with_retries(
            self.service.files()
            .get(fileId=file_id, fields="sha256Checksum")
            .execute
        )
        return res.get("sha256Checksum")

    # -- trash (never hard-delete) ---------------------------------------
    def trash(self, file_id: str) -> None:
        with_retries(
            self.service.files()
            .update(fileId=file_id, body={"trashed": True})
            .execute
        )


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(4 * 1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()

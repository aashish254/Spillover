"""Ingest a Google Takeout export of Google Photos.

Takeout can't be fetched via API, so the user downloads the archive(s) from
takeout.google.com themselves, extracts them, and points Spillover at the
folder. This module pairs each media file with its Takeout JSON sidecar
(which holds the original capture date, GPS, album info, etc.) and returns
upload-ready items.

Takeout layout (typical):
    ~/takeout/Takeout/Google Photos/Photos from 2023/IMG_1234.jpg
    ~/takeout/Takeout/Google Photos/Photos from 2023/IMG_1234.jpg.json
"""

from __future__ import annotations

import json
import zipfile
from dataclasses import dataclass
from pathlib import Path

from tqdm import tqdm

MEDIA_SUFFIXES = {
    ".jpg", ".jpeg", ".png", ".heic", ".heif", ".webp", ".gif", ".bmp",
    ".tiff", ".dng", ".raw", ".arw", ".cr2", ".nef",
    ".mp4", ".mov", ".avi", ".mkv", ".webm", ".m4v", ".3gp",
}


@dataclass
class TakeoutItem:
    path: Path
    meta: dict
    album: str  # e.g. "Photos from 2023" — becomes the destination subfolder


def extract_archives(takeout_dir: Path, dest: Path | None = None) -> Path:
    """Unzip every takeout-*.zip part into dest (default: <dir>/extracted)."""
    takeout_dir = Path(takeout_dir)
    dest = dest or (takeout_dir / "extracted")
    dest.mkdir(parents=True, exist_ok=True)
    zips = sorted(takeout_dir.glob("takeout-*.zip"))
    if not zips:
        # Maybe the user already extracted it themselves.
        return takeout_dir
    for z in tqdm(zips, desc="Extracting Takeout archives", unit="zip"):
        with zipfile.ZipFile(z) as zf:
            zf.extractall(dest)
    return dest


def collect_items(root: Path) -> list[TakeoutItem]:
    """Walk an extracted Takeout tree and pair media with their .json sidecars."""
    root = Path(root)
    # Find the Google Photos subtree if present.
    candidates = list(root.rglob("Google Photos"))
    base = candidates[0] if candidates else root

    items: list[TakeoutItem] = []
    for path in base.rglob("*"):
        if not path.is_file():
            continue
        if path.suffix.lower() not in MEDIA_SUFFIXES:
            continue
        if path.name.endswith(".json"):
            continue
        meta: dict = {}
        sidecar = path.with_name(path.name + ".json")
        # Takeout truncates long filenames in sidecars; try the truncated form.
        if not sidecar.exists():
            trunc = path.with_name(path.name[:46] + ".json")
            sidecar = trunc if trunc.exists() else sidecar
        if sidecar.exists():
            try:
                meta = json.loads(sidecar.read_text())
            except Exception:
                meta = {}
        # Album = the immediate "Photos from YYYY" folder, for tidy destinations.
        album = path.parent.name if path.parent != base else "Google Photos"
        items.append(TakeoutItem(path=path, meta=meta, album=album))
    return items


def summarize(items: list[TakeoutItem]) -> dict:
    total = sum(i.path.stat().st_size for i in items)
    by_album: dict[str, int] = {}
    for i in items:
        by_album[i.album] = by_album.get(i.album, 0) + 1
    return {"files": len(items), "bytes": total, "albums": by_album}

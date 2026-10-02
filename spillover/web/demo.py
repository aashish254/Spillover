"""Demo provider: realistic fake data so the UI can be reviewed without Google creds."""

from __future__ import annotations

import random
import time

from .. import photos, sharing

GB = 1024 ** 3


def _files_for_main() -> list[dict]:
    rng = random.Random(42)
    files: list[dict] = []
    n = 0

    def add(name, size, mime, days_ago):
        nonlocal n
        n += 1
        files.append({
            "id": f"demo{1000 + n}",
            "name": name,
            "mimeType": mime,
            "size": size,
            "modifiedTime": time.strftime(
                "%Y-%m-%dT%H:%M:%S.000Z",
                time.gmtime(time.time() - days_ago * 86400),
            ),
            "sha256": None,
            # Sharing metadata — most files are sole-owned; two are
            # deliberately not, to demo the safety warnings.
            "shared": False,
            "owner_emails": ["aashish.main@gmail.com"],
            "owned_by_me": True,
        })

    for i in range(1, 13):
        add(f"IMG_2024_{i:04d}.jpg", rng.randint(2, 9) * 1024**2,
            "image/jpeg", rng.randint(100, 700))
    for i in range(1, 6):
        add(f"VID_2024_{i:04d}.mp4", rng.randint(300, 1800) * 1024**2,
            "video/mp4", rng.randint(60, 400))
    add("iceland_trip_day3.mp4", int(2.4 * GB), "video/mp4", 210)
    add("wedding_full_ceremony.mp4", int(4.1 * GB), "video/mp4", 320)
    add("drone_beach_4k.mp4", int(3.2 * GB), "video/mp4", 150)
    add("portfolio_final_v7.psd", int(1.1 * GB), "image/vnd.adobe.photoshop", 45)
    add("tax_documents_2023.zip", int(0.8 * GB), "application/zip", 500)
    add("phone_backup_march.zip", int(2.8 * GB), "application/zip", 190)
    add("design_system_figma_export.zip", int(0.4 * GB), "application/zip", 30)
    for i in range(1, 9):
        add(f"screenshot_2024_{i:02d}.png", rng.randint(1, 6) * 1024**2,
            "image/png", rng.randint(10, 200))
    for i in range(1, 7):
        add(f"recording_voice_memo_{i:02d}.m4a", rng.randint(8, 60) * 1024**2,
            "audio/mp4", rng.randint(20, 300))
    add("contract_signed.pdf", 4 * 1024**2, "application/pdf", 260)
    add("resume_2025_final.pdf", 2 * 1024**2, "application/pdf", 90)

    # portfolio_final_v7.psd is shared with a collaborator → warning demo.
    for f in files:
        if f["name"] == "portfolio_final_v7.psd":
            f["shared"] = True
            f["owner_emails"] = ["aashish.main@gmail.com", "teammate@example.com"]
    # contract_signed.pdf is owned by someone else → blocked demo.
    for f in files:
        if f["name"] == "contract_signed.pdf":
            f["owned_by_me"] = False
            f["owner_emails"] = ["lawyer@example.com"]
    return files


_ACCOUNTS = [
    {"alias": "main", "email": "aashish.main@gmail.com",
     "limit": 15 * GB, "usage": int(14.2 * GB), "drive": int(13.9 * GB)},
    {"alias": "backup-1", "email": "aashish.backup1@gmail.com",
     "limit": 15 * GB, "usage": int(3.1 * GB), "drive": int(2.9 * GB)},
    {"alias": "backup-2", "email": "aashish.backup2@gmail.com",
     "limit": 15 * GB, "usage": int(5.8 * GB), "drive": int(5.5 * GB)},
    {"alias": "media-vault", "email": "aashish.media@gmail.com",
     "limit": 15 * GB, "usage": int(1.2 * GB), "drive": int(1.1 * GB)},
]

# Some of main's files were copied into the backup accounts years ago and left
# there — the waste the Duplicates view exists to find. backup-2 is deliberately
# unscanned so the UI has to show a partial-scan state.
_DUP_PLAN = {
    "backup-1": ["wedding_full_ceremony.mp4", "phone_backup_march.zip",
                 "tax_documents_2023.zip", "VID_2024_0002.mp4"],
    "media-vault": ["iceland_trip_day3.mp4", "drone_beach_4k.mp4",
                    "wedding_full_ceremony.mp4", "portfolio_final_v7.psd",
                    "recording_voice_memo_03.m4a"],
}

_demo_trashed: set[str] = set()


def _files_for(alias: str) -> list[dict]:
    if alias == "main":
        return _files_for_main()
    email = next(a["email"] for a in _ACCOUNTS if a["alias"] == alias)
    by_name = {f["name"]: f for f in _files_for_main()}
    out = []
    for i, name in enumerate(_DUP_PLAN.get(alias, [])):
        f = dict(by_name[name])
        f["id"] = f"demo{alias.replace('-', '')}{i}"
        f["owner_emails"] = [email]
        out.append(f)
    return out


class _DemoPickerSession:
    """Stands in for PickerAPI: the demo session is always settled."""

    def get_session(self, session_id):
        return {"id": session_id, "mediaItemsSet": True}


class DemoProvider:
    """Pretends to be Google: instant quotas, canned scans, fake runs."""

    name = "demo"

    def list_accounts(self) -> list[dict]:
        return [
            {"alias": a["alias"], "email": a["email"],
             "quota": {"limit": a["limit"], "usage": a["usage"],
                       "drive_usage": a["drive"],
                       "free": a["limit"] - a["usage"]}}
            for a in _ACCOUNTS
        ]

    def connect(self, alias: str) -> dict:
        raise RuntimeError("Demo mode: connect a real account from the CLI.")

    def disconnect(self, alias: str) -> None:
        raise RuntimeError("Demo mode is read-only for accounts.")

    def scan_summary(self, alias: str) -> dict:
        if alias != "main":
            return {"alias": alias, "scanned_at": int(time.time()),
                    "total_files": 0, "quota_files": 0, "quota_bytes": 0,
                    "top_extensions": [], "largest": []}
        files = _files_for_main()
        from collections import Counter
        from pathlib import Path
        by_ext: Counter = Counter()
        for f in files:
            by_ext[Path(f["name"]).suffix.lower() or "(no ext)"] += f["size"]
        return {
            "alias": alias,
            "scanned_at": int(time.time()),
            "total_files": len(files),
            "quota_files": len(files),
            "quota_bytes": sum(f["size"] for f in files),
            "top_extensions": by_ext.most_common(10),
            "largest": sorted(files, key=lambda f: f["size"], reverse=True)[:20],
        }

    def get_files(self, alias: str) -> list[dict]:
        return _files_for_main() if alias == "main" else []

    def scans(self) -> dict[str, list[dict]]:
        """Cached scans for the accounts the demo has 'scanned'."""
        out = {}
        for a in _ACCOUNTS:
            alias = a["alias"]
            if alias != "main" and alias not in _DUP_PLAN:
                continue
            out[alias] = [f for f in _files_for(alias)
                          if f["id"] not in _demo_trashed]
        return out

    def duplicates(self) -> dict:
        from .server import _group_duplicates
        return _group_duplicates(self.scans())

    def trash_file(self, alias: str, file_id: str) -> None:
        _demo_trashed.add(file_id)

    def run_plan(self, plan: dict, progress_cb=None) -> dict:
        total = len(plan["assignments"])
        done = skipped = 0
        for i, a in enumerate(plan["assignments"], start=1):
            # Demo mirrors the real engine: shared/not-owned files are
            # skipped unless the plan explicitly includes them.
            if (not plan.get("include_shared")
                    and not sharing.is_movable_by_default(a["file"])):
                skipped += 1
                if progress_cb:
                    progress_cb(i, total, {
                        "name": a["file"]["name"],
                        "dest_alias": a["dest_alias"],
                        "status": "skipped",
                        "note": "shared file skipped by default",
                    })
                continue
            if progress_cb:
                progress_cb(i, total, {
                    "name": a["file"]["name"], "dest_alias": a["dest_alias"],
                    "status": "done", "note": "",
                })
            done += 1
        return {"done": done, "failed": 0, "skipped": skipped,
                "bytes_moved": sum(a["file"]["size"] for a in plan["assignments"])}

    # -- auth status & reconnect (demo) --------------------------------
    def auth_status(self) -> dict:
        out = {}
        for a in _ACCOUNTS:
            broken = a["alias"] == "backup-2"  # demo a revoked token
            out[a["alias"]] = {
                "email": a["email"],
                "ok": not broken,
                "needs_reconnect": broken,
                "error": ("Google rejected the stored token (expired or "
                          "revoked).") if broken else "",
            }
        return out

    def reconnect(self, alias: str) -> dict:
        time.sleep(1.5)  # pretend the browser sign-in happened
        acct = next((a for a in _ACCOUNTS if a["alias"] == alias), None)
        return {"alias": alias,
                "email": acct["email"] if acct else f"{alias}@example.com"}

    # -- crash resume (demo) --------------------------------------------
    def interrupted_transfers(self) -> list[dict]:
        if getattr(self, "_resumed", False):
            return []
        return [{
            "id": 9001,
            "filename": "drone_beach_4k.mp4",
            "size_bytes": int(3.2 * GB),
            "sha256": None,
            "mime_type": "video/mp4",
            "src_alias": "main",
            "src_file_id": "demo9999",
            "dst_alias": "backup-1",
            "dst_file_id": None,
            "run_id": "demo-run",
            "state": "in_progress",
            "stage": "downloaded",
            "created_at": int(time.time()) - 3600,
            "updated_at": int(time.time()) - 3600,
            "note": "",
        }]

    def resume(self, progress_cb=None) -> dict:
        self._resumed = True
        items = [{
            "id": 9001,
            "filename": "drone_beach_4k.mp4",
            "size_bytes": int(3.2 * GB),
            "src_alias": "main",
            "dst_alias": "backup-1",
        }]
        for i, t in enumerate(items, start=1):
            time.sleep(0.4)
            if progress_cb:
                progress_cb(i, len(items), {
                    "name": t["filename"], "dest_alias": t["dst_alias"],
                    "status": "done", "note": "resumed from download stage",
                })
        return {"resumed": len(items), "done": len(items), "failed": 0,
                "skipped": 0,
                "bytes_moved": sum(t["size_bytes"] for t in items)}

    def shared_files(self, alias: str) -> list[dict]:
        return sharing.warnings(self.get_files(alias))

    # -- photos picker (demo) -------------------------------------------
    def photos_create_session(self, alias: str) -> dict:
        return {
            "session_id": "demo-picker-session",
            "picker_uri": "https://photos.google.com/picker/demo (demo link)",
            "expires": "",
        }

    def photos_list_picked(self, alias: str, session_id: str) -> list[dict]:
        # Exercise the same settle-check the real provider uses.
        photos.check_selection_done(alias, session_id,
                                    picker=_DemoPickerSession())
        return [
            {"id": "demo-photo-1", "filename": "IMG_2024_beach.jpg",
             "mimeType": "image/jpeg", "createTime": "2024-06-12",
             "type": "PHOTO"},
            {"id": "demo-photo-2", "filename": "VID_2024_birthday.mp4",
             "mimeType": "video/mp4", "createTime": "2024-07-03",
             "type": "VIDEO"},
            {"id": "demo-photo-3", "filename": "IMG_2024_mountains.jpg",
             "mimeType": "image/jpeg", "createTime": "2024-08-21",
             "type": "PHOTO"},
        ]

    def photos_transfer(self, alias, session_id, dest_aliases, progress_cb=None):
        items = self.photos_list_picked(alias, session_id)
        for i, it in enumerate(items, start=1):
            time.sleep(0.4)
            if progress_cb:
                progress_cb(i, len(items), {
                    "name": it["filename"],
                    "dest_alias": dest_aliases[(i - 1) % len(dest_aliases)],
                    "status": "done", "note": "",
                })
        return {"done": len(items), "failed": 0,
                "bytes": sum(4_000_000 for _ in items),
                "manifest": "demo-manifest.md",
                "manifest_path": "(demo)"}

    def ledger(self, query: str = "", limit: int = 50) -> list[dict]:
        rows = [
            {"filename": "wedding_full_ceremony.mp4", "size_bytes": int(4.1 * GB),
             "src_alias": "main", "dst_alias": "media-vault",
             "status": "done", "moved_at": "2026-09-28 18:02"},
            {"filename": "iceland_trip_day3.mp4", "size_bytes": int(2.4 * GB),
             "src_alias": "main", "dst_alias": "backup-2",
             "status": "done", "moved_at": "2026-09-28 17:41"},
            {"filename": "drone_beach_4k.mp4", "size_bytes": int(3.2 * GB),
             "src_alias": "main", "dst_alias": "backup-1",
             "status": "done", "moved_at": "2026-09-21 11:15"},
            {"filename": "phone_backup_march.zip", "size_bytes": int(2.8 * GB),
             "src_alias": "main", "dst_alias": "backup-1",
             "status": "failed", "moved_at": "2026-09-21 10:58",
             "note": "upload stalled at 61%"},
        ]
        if query:
            q = query.lower()
            rows = [r for r in rows if q in r["filename"].lower()]
        return rows[:limit]

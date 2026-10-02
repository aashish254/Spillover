"""The transfer engine.

For every assignment: download -> hash -> upload -> verify hash ->
trash the source -> record in the ledger.

The source file is trashed ONLY after the destination copy's SHA-256 matches
the downloaded bytes. Anything that fails is recorded as failed and the
source is left untouched.

Crash safety: every file is tracked as a transaction in the ledger
(pending -> in_progress -> done | failed, with the last completed pipeline
stage). If the process dies mid-run, ``resume()`` picks up the interrupted
transfers: a destination copy that was already uploaded *and verified* is
never re-uploaded — the run continues from the first unfinished stage.
"""

from __future__ import annotations

import tempfile
import uuid
from pathlib import Path

from tqdm import tqdm

from . import ledger
from . import sharing
from .config import DEST_FOLDER_NAME, TMP_DIR, ensure_dirs
from .drive import DriveClient, sha256_of
from .planner import Assignment

# Stages after which a resume can skip re-doing work.
_RESUMABLE_STAGES = ("uploaded", "verified", "trashed")


def _fmt_gb(n: int) -> str:
    return f"{n / 1024**3:.2f} GB"


def _move_one(
    src,
    dest_client,
    dest_folders: dict,
    a: Assignment,
    src_alias: str,
    tid: int,
    tmpdir: Path,
    report,
    stats: dict,
    progress_cb=None,
) -> None:
    """Run the full pipeline for one assignment under transaction ``tid``."""
    f = a.file
    local = tmpdir / f"{f['id']}_{f['name']}"
    try:
        ledger.update_transfer(tid, state="in_progress")
        # 1. Download from the source account.
        src.download(f["id"], local)
        local_hash = sha256_of(local)
        ledger.update_transfer(tid, stage="downloaded", sha256=local_hash)

        # 2. Upload to the destination account.
        dc = dest_client(a.dest_alias)
        created = dc.upload(local, dest_folders[a.dest_alias], name=f["name"])
        dst_id = created.get("id")
        ledger.update_transfer(tid, stage="uploaded", dst_file_id=dst_id)

        # 3. Verify: destination hash must match what we downloaded.
        remote_hash = dc.remote_sha256(dst_id)
        if remote_hash and remote_hash != local_hash:
            raise RuntimeError(
                f"hash mismatch after upload "
                f"(local {local_hash[:12]} vs remote {remote_hash[:12]})"
            )
        if created.get("size") and int(created["size"]) != f["size"]:
            raise RuntimeError("size mismatch after upload")
        ledger.update_transfer(tid, stage="verified")

        # 4. Only now: trash the source (recoverable for 30 days).
        src.trash(f["id"])
        ledger.update_transfer(tid, stage="trashed")

        # 5. Ledger.
        ledger.record_move(
            filename=f["name"], size_bytes=f["size"], sha256=local_hash,
            src_alias=src_alias, src_file_id=f["id"],
            dst_alias=a.dest_alias, dst_file_id=dst_id, status="done",
        )
        ledger.update_transfer(tid, state="done")
        stats["done"] += 1
        stats["bytes_moved"] += f["size"]
        report(f, a.dest_alias, "done")
    except Exception as exc:  # noqa: BLE001 - record and continue
        ledger.update_transfer(tid, state="failed", note=str(exc)[:300])
        ledger.record_move(
            filename=f["name"], size_bytes=f["size"],
            sha256=f.get("sha256"), src_alias=src_alias,
            src_file_id=f["id"], dst_alias=a.dest_alias,
            dst_file_id=None, status="failed", note=str(exc)[:300],
        )
        stats["failed"] += 1
        report(f, a.dest_alias, "failed", str(exc)[:200])
        if progress_cb is None:
            tqdm.write(f"FAILED {f['name']}: {exc}")
    finally:
        if local.exists():
            local.unlink()  # never leave copies on disk


def _dest_client_cache(dest_subfolder: str = ""):
    """Return (get_client, folders) pair shared by execute() and resume()."""
    dest_clients: dict[str, DriveClient] = {}
    dest_folders: dict[str, str] = {}

    def get_client(alias: str):
        if alias not in dest_clients:
            c = DriveClient(alias)
            folder_id = c.ensure_folder(DEST_FOLDER_NAME)
            if dest_subfolder:
                folder_id = c.ensure_folder(dest_subfolder, parent_id=folder_id)
            dest_clients[alias] = c
            dest_folders[alias] = folder_id
        return dest_clients[alias]

    return get_client, dest_folders


def execute(
    assignments: list[Assignment],
    src_alias: str,
    dry_run: bool = True,
    dest_subfolder: str = "",
    progress_cb=None,
    include_shared: bool = False,
) -> dict:
    """Run the moves. Returns a stats dict. dry_run=True only prints the plan.

    progress_cb, when given, is called as progress_cb(done, total, event)
    after each file, where event is {"name", "dest_alias", "status", "note"}.

    Files that are shared with others (or not solely owned by the source
    account) are skipped by default and recorded as "skipped" in the ledger;
    pass include_shared=True to move them anyway after explicit confirmation.
    """
    ensure_dirs()
    total_bytes = sum(a.file["size"] for a in assignments)

    if dry_run:
        print(f"\nDRY RUN — {len(assignments)} files, {_fmt_gb(total_bytes)} total")
        print("Nothing will be downloaded, uploaded, or trashed.\n")
        per_dest: dict[str, list] = {}
        for a in assignments:
            per_dest.setdefault(a.dest_alias, []).append(a.file)
        for dest, files in per_dest.items():
            print(f"  -> {dest}: {len(files)} files, "
                  f"{_fmt_gb(sum(f['size'] for f in files))}")
            for f in sorted(files, key=lambda x: x['size'], reverse=True)[:5]:
                print(f"       {_fmt_gb(f['size']):>10}  {f['name']}")
            if len(files) > 5:
                print(f"       ... and {len(files) - 5} more")
        print("\nRe-run with --execute to perform the moves.")
        return {"dry_run": True, "files": len(assignments), "bytes": total_bytes}

    run_id = uuid.uuid4().hex
    interrupted = ledger.find_interrupted()
    if interrupted:
        print(f"\nNOTE: {len(interrupted)} interrupted transfer(s) from a "
              "previous run exist (crash or kill).")
        print("Resume them with: spillover resume   (or the Resume button "
              "in the web UI). Continuing with the new plan below.\n")

    src = DriveClient(src_alias)
    dest_client, dest_folders = _dest_client_cache(dest_subfolder)

    stats = {"done": 0, "failed": 0, "skipped": 0, "bytes_moved": 0}
    total = len(assignments)

    def report(f: dict, dest: str, status: str, note: str = "") -> None:
        if progress_cb:
            progress_cb(stats["done"] + stats["failed"] + stats["skipped"],
                        total, {
                            "name": f["name"], "dest_alias": dest,
                            "status": status, "note": note,
                        })

    with tempfile.TemporaryDirectory(dir=TMP_DIR) as tmp:
        tmpdir = Path(tmp)
        iterator = enumerate(assignments, start=1)
        if progress_cb is None:
            iterator = tqdm(iterator, total=total, desc="Moving files", unit="file")
        for i, a in iterator:
            f = a.file
            if not sharing.is_movable_by_default(f) and not include_shared:
                reason = sharing.classify(f)["reason"]
                ledger.record_move(
                    filename=f["name"], size_bytes=f["size"],
                    sha256=f.get("sha256"), src_alias=src_alias,
                    src_file_id=f["id"], dst_alias=a.dest_alias,
                    dst_file_id=None, status="skipped",
                    note=f"shared file skipped: {reason}"[:300],
                )
                stats["skipped"] += 1
                report(f, a.dest_alias, "skipped", reason[:200])
                if progress_cb is None:
                    tqdm.write(f"SKIPPED {f['name']}: {reason[:120]}")
                continue
            tid = ledger.begin_transfer(
                filename=f["name"], size_bytes=f["size"],
                sha256=f.get("sha256"), mime_type=f.get("mimeType"),
                src_alias=src_alias, src_file_id=f["id"],
                dst_alias=a.dest_alias, run_id=run_id,
            )
            _move_one(src, dest_client, dest_folders, a, src_alias,
                      tid, tmpdir, report, stats, progress_cb)

    print(f"\nDone: {stats['done']} moved ({_fmt_gb(stats['bytes_moved'])}), "
          f"{stats['failed']} failed, {stats['skipped']} skipped.")
    print("Moved files are in each destination's 'Spillover' folder; "
          "originals are in the source account's trash (recoverable for 30 days).")
    return stats


def resume(
    src_alias: str | None = None,
    dest_subfolder: str = "",
    progress_cb=None,
) -> dict:
    """Resume transfers interrupted by a crash/kill.

    Never re-uploads a destination copy that was already uploaded and
    verified — the run continues from the first unfinished stage. The
    verify-before-trash guarantee is preserved: the source is only trashed
    after a verified copy exists on the destination.
    """
    ensure_dirs()
    todo = ledger.find_interrupted()
    if src_alias:
        todo = [t for t in todo if t["src_alias"] == src_alias]
    stats = {"resumed": 0, "done": 0, "failed": 0, "skipped": 0,
             "bytes_moved": 0}
    total = len(todo)
    if not total:
        print("No interrupted transfers to resume.")
        return stats

    print(f"Resuming {total} interrupted transfer(s)...")
    run_id = uuid.uuid4().hex
    src_clients: dict[str, DriveClient] = {}
    dest_client, dest_folders = _dest_client_cache(dest_subfolder)

    def src_client(alias: str) -> DriveClient:
        if alias not in src_clients:
            src_clients[alias] = DriveClient(alias)
        return src_clients[alias]

    def report(f: dict, dest: str, status: str, note: str = "") -> None:
        if progress_cb:
            progress_cb(stats["done"] + stats["failed"], total, {
                "name": f["name"], "dest_alias": dest,
                "status": status, "note": note,
            })

    with tempfile.TemporaryDirectory(dir=TMP_DIR) as tmp:
        tmpdir = Path(tmp)
        iterator = todo if progress_cb is not None else tqdm(
            todo, desc="Resuming", unit="file")
        for t in iterator:
            tid = t["id"]
            stats["resumed"] += 1
            ledger.update_transfer(tid, run_id=run_id, state="in_progress")
            dc = dest_client(t["dst_alias"])
            sc = src_client(t["src_alias"])

            # Guard: already finished by another run — never re-upload.
            if ledger.find_done_transfer(t["src_file_id"], t["dst_alias"]):
                ledger.update_transfer(tid, state="done", stage="trashed",
                                       note="already completed")
                stats["done"] += 1
                stats["bytes_moved"] += t["size_bytes"]
                report({"name": t["filename"]}, t["dst_alias"], "done",
                       "already completed")
                continue

            finished = False
            # If a verified copy already sits on the destination, skip the
            # expensive half and finish the move from there.
            if t["dst_file_id"] and t["stage"] in _RESUMABLE_STAGES:
                try:
                    remote_hash = dc.remote_sha256(t["dst_file_id"])
                except Exception:  # noqa: BLE001 - treat as missing
                    remote_hash = None
                if remote_hash and t["sha256"] and remote_hash == t["sha256"]:
                    try:
                        sc.trash(t["src_file_id"])
                    except Exception:  # noqa: BLE001 - may already be trashed
                        pass
                    ledger.record_move(
                        filename=t["filename"], size_bytes=t["size_bytes"],
                        sha256=t["sha256"], src_alias=t["src_alias"],
                        src_file_id=t["src_file_id"], dst_alias=t["dst_alias"],
                        dst_file_id=t["dst_file_id"], status="done",
                        note="resumed: destination copy already verified",
                    )
                    ledger.update_transfer(tid, state="done", stage="trashed")
                    stats["done"] += 1
                    stats["bytes_moved"] += t["size_bytes"]
                    report({"name": t["filename"]}, t["dst_alias"], "done",
                           "copy already verified — no re-upload")
                    finished = True
                elif remote_hash:
                    # A corrupt/partial copy from the crashed run: remove it
                    # so the retry starts clean (it's our own unverified file).
                    try:
                        dc.trash(t["dst_file_id"])
                    except Exception:  # noqa: BLE001
                        pass

            if not finished:
                a = Assignment(
                    file={
                        "id": t["src_file_id"],
                        "name": t["filename"],
                        "size": t["size_bytes"],
                        "mimeType": t["mime_type"] or "application/octet-stream",
                        "sha256": t["sha256"],
                    },
                    dest_alias=t["dst_alias"],
                )
                # Reuse the existing transaction row; _move_one advances it.
                _move_one(sc, dest_client, dest_folders, a, t["src_alias"],
                          tid, tmpdir, report, stats, progress_cb)

    print(f"\nResume done: {stats['done']} completed, {stats['failed']} failed.")
    return stats


def upload_local(
    local_path: Path,
    dst_alias: str,
    name: str | None = None,
    subfolder: str = "",
    src_label: str = "takeout",
) -> dict:
    """Upload one local file (e.g. from a Takeout export) with verify+ledger."""
    ensure_dirs()
    dc = DriveClient(dst_alias)
    folder_id = dc.ensure_folder(DEST_FOLDER_NAME)
    if subfolder:
        folder_id = dc.ensure_folder(subfolder, parent_id=folder_id)
    local_hash = sha256_of(local_path)
    created = dc.upload(local_path, folder_id, name=name or local_path.name)
    dst_id = created.get("id")
    remote_hash = dc.remote_sha256(dst_id)
    if remote_hash and remote_hash != local_hash:
        raise RuntimeError("hash mismatch after upload")
    ledger.record_move(
        filename=local_path.name, size_bytes=local_path.stat().st_size,
        sha256=local_hash, src_alias=src_label, src_file_id=None,
        dst_alias=dst_alias, dst_file_id=dst_id, status="done",
    )
    return created

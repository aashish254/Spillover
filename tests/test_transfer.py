"""Transfer-engine tests: transaction tracking, shared-file skipping, resume.

Uses fake Drive clients — no network, no credentials.
"""

import hashlib
import os
import sys
import tempfile
from pathlib import Path

_tmp = tempfile.mkdtemp()
os.environ["HOME"] = _tmp  # must precede importing spillover.config

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest  # noqa: E402

from spillover import ledger, transfer  # noqa: E402
from spillover.planner import Assignment  # noqa: E402


class FakeDrive:
    """In-memory stand-in for DriveClient. One instance per alias."""

    instances: dict = {}

    def __new__(cls, alias):
        if alias in cls.instances:
            return cls.instances[alias]
        inst = super().__new__(cls)
        cls.instances[alias] = inst
        return inst

    def __init__(self, alias):
        if getattr(self, "_ready", False):
            return
        self.alias = alias
        self.files: dict = {}  # id -> {"content": bytes, "trashed": bool}
        self.upload_count = 0
        self._ready = True

    # -- fake content helpers ---------------------------------------
    def seed(self, file_id: str, name: str, content: bytes):
        self.files[file_id] = {"name": name, "content": content,
                               "trashed": False}

    # -- DriveClient API --------------------------------------------
    def download(self, file_id, local: Path):
        local.write_bytes(self.files[file_id]["content"])

    def upload(self, local: Path, folder_id, name=None):
        data = local.read_bytes()
        new_id = f"{self.alias}-up-{self.upload_count}"
        self.upload_count += 1
        self.files[new_id] = {"name": name or local.name, "content": data,
                              "trashed": False}
        return {"id": new_id, "size": str(len(data))}

    def remote_sha256(self, file_id):
        return hashlib.sha256(self.files[file_id]["content"]).hexdigest()

    def trash(self, file_id):
        self.files[file_id]["trashed"] = True

    def ensure_folder(self, name, parent_id=None):
        return f"folder-{self.alias}-{name}"


@pytest.fixture(autouse=True)
def fake_drive(monkeypatch):
    FakeDrive.instances = {}
    monkeypatch.setattr(transfer, "DriveClient", FakeDrive)
    # Fresh ledger per test: the DB file is shared within this module.
    # (_connect re-applies the schema, so this works on a fresh file too.)
    with ledger._connect() as conn:
        conn.execute("DELETE FROM transfers")
        conn.execute("DELETE FROM moves")
    yield


_uid = [0]


def _file(name="video.mp4", size=64, **kw):
    _uid[0] += 1
    f = {"id": f"src-{_uid[0]}-{name}", "name": name, "size": size,
         "mimeType": "video/mp4", "sha256": None,
         "shared": False, "owner_emails": ["me@x.com"], "owned_by_me": True}
    f.update(kw)
    # Keep an explicit id override working if a test passes one.
    return f


def _seed_src(alias, files):
    drv = FakeDrive(alias)
    for f in files:
        drv.seed(f["id"], f["name"], os.urandom(f["size"]))
    return drv


def _assignment(f, dest="dst"):
    return Assignment(file=f, dest_alias=dest)


# -- transaction tracking ---------------------------------------------

def test_execute_tracks_transactions():
    files = [_file("a.mp4"), _file("b.mp4")]
    src = _seed_src("main", files)
    stats = transfer.execute([_assignment(f) for f in files], "main",
                             dry_run=False)
    assert stats == {"done": 2, "failed": 0, "skipped": 0,
                     "bytes_moved": sum(f["size"] for f in files)}
    assert stats["bytes_moved"] == sum(f["size"] for f in files)
    # Every transaction reached done/trashed.
    for t in ledger.find_interrupted():
        raise AssertionError(f"interrupted transfer left behind: {t}")
    assert ledger.transfer_stats() == {"done": 2}
    # Source files trashed, nothing left on local disk.
    assert all(src.files[f["id"]]["trashed"] for f in files)


def test_failed_transfer_leaves_source_untouched():
    files = [_file("good.mp4")]
    src = _seed_src("main", files)

    class ExplodingDrive(FakeDrive):
        def upload(self, local, folder_id, name=None):
            raise RuntimeError("network died")

    FakeDrive.instances["dst"] = ExplodingDrive("dst")
    stats = transfer.execute([_assignment(f) for f in files], "main",
                             dry_run=False)
    assert stats["done"] == 0 and stats["failed"] == 1
    assert not src.files[files[0]["id"]]["trashed"]
    assert ledger.transfer_stats() == {"failed": 1}


# -- shared-file skipping ---------------------------------------------

def test_shared_files_skipped_by_default():
    sole = _file("sole.mp4")
    shared = _file("shared.mp4", shared=True,
                   owner_emails=["me@x.com", "pal@x.com"])
    not_owned = _file("theirs.mp4", owned_by_me=False,
                      owner_emails=["boss@x.com"])
    src = _seed_src("main", [sole, shared, not_owned])
    stats = transfer.execute(
        [_assignment(f) for f in [sole, shared, not_owned]], "main",
        dry_run=False)
    assert stats["done"] == 1 and stats["skipped"] == 2
    assert not src.files[shared["id"]]["trashed"]
    assert not src.files[not_owned["id"]]["trashed"]
    assert src.files[sole["id"]]["trashed"]
    # Skips recorded in the ledger so the user can review them.
    skipped = [r for r in ledger.recent(50) if r["status"] == "skipped"]
    assert len(skipped) == 2


def test_include_shared_moves_them_after_explicit_opt_in():
    shared = _file("shared.mp4", shared=True,
                   owner_emails=["me@x.com", "pal@x.com"])
    src = _seed_src("main", [shared])
    stats = transfer.execute([_assignment(shared)], "main", dry_run=False,
                             include_shared=True)
    assert stats["done"] == 1 and stats["skipped"] == 0
    assert src.files[shared["id"]]["trashed"]


def test_old_scan_files_without_sharing_metadata_still_move():
    # Scans from before sharing metadata existed: warn, but don't block.
    old = {"id": "old-1", "name": "legacy.mp4", "size": 64,
           "mimeType": "video/mp4", "sha256": None}
    src = _seed_src("main", [old])
    stats = transfer.execute([_assignment(old)], "main", dry_run=False)
    assert stats["done"] == 1 and stats["skipped"] == 0
    assert src.files[old["id"]]["trashed"]


# -- crash resume ------------------------------------------------------

def _crash_mid_run():
    """Simulate a run that died after verifying one file's upload."""
    files = [_file("x.mp4"), _file("y.mp4")]
    src = _seed_src("main", files)
    dst = FakeDrive("dst")
    content = src.files[files[0]["id"]]["content"]
    digest = hashlib.sha256(content).hexdigest()
    # File 1: fully uploaded + verified, then the process died before trash.
    dst.seed("dst-copy-x", files[0]["name"], content)
    ledger.begin_transfer(
        filename=files[0]["name"], size_bytes=files[0]["size"],
        sha256=digest, mime_type="video/mp4",
        src_alias="main", src_file_id=files[0]["id"],
        dst_alias="dst", run_id="crashed-run")
    tid1 = ledger.find_interrupted()[0]["id"]
    ledger.update_transfer(tid1, state="in_progress", stage="verified",
                           dst_file_id="dst-copy-x")
    # File 2: died mid-download, nothing on the destination.
    tid2 = ledger.begin_transfer(
        filename=files[1]["name"], size_bytes=files[1]["size"],
        sha256=None, mime_type="video/mp4",
        src_alias="main", src_file_id=files[1]["id"],
        dst_alias="dst", run_id="crashed-run")
    ledger.update_transfer(tid2, state="in_progress", stage="downloaded")
    return src, dst, files, digest


def test_resume_skips_reupload_of_verified_copy():
    src, dst, files, digest = _crash_mid_run()
    before_uploads = dst.upload_count
    stats = transfer.resume()
    assert stats["done"] == 2 and stats["failed"] == 0
    # The already-verified copy was NOT re-uploaded.
    assert dst.upload_count == before_uploads + 1  # only file 2 uploaded
    # Both sources trashed only after verified copies existed.
    assert all(src.files[f["id"]]["trashed"] for f in files)
    assert ledger.find_interrupted() == []


def test_resume_redoes_corrupt_partial_upload():
    src, dst, files, digest = _crash_mid_run()
    # The crashed run's destination copy is corrupt — resume must replace it.
    dst.files["dst-copy-x"]["content"] = b"corrupt-partial-bytes"
    stats = transfer.resume()
    assert stats["done"] == 2
    good = [c for c in dst.files.values()
            if c["content"] == src.files[files[0]["id"]]["content"]
            and not c["trashed"]]
    assert good, "no intact destination copy for file 1 after resume"


def test_resume_nothing_to_do():
    assert transfer.resume() == {"resumed": 0, "done": 0, "failed": 0,
                                 "skipped": 0, "bytes_moved": 0}

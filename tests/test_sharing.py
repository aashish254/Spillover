"""Sharing-classification tests: shared/not-owned files must be flagged."""

import os
import sys
import tempfile
from pathlib import Path

_tmp = tempfile.mkdtemp()
os.environ["HOME"] = _tmp  # must precede importing spillover.config

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from spillover import sharing  # noqa: E402


def sole_file():
    return {
        "id": "a", "name": "video.mp4", "size": 100,
        "shared": False, "owner_emails": ["me@x.com"], "owned_by_me": True,
    }


def test_sole_is_movable_without_warnings():
    f = sole_file()
    assert sharing.classify(f)["level"] == sharing.SOLE
    assert sharing.is_movable_by_default(f)
    assert sharing.warnings([f]) == []


def test_shared_file_is_blocked_by_default():
    f = sole_file()
    f["shared"] = True
    f["owner_emails"] = ["me@x.com", "friend@x.com"]
    c = sharing.classify(f)
    assert c["level"] == sharing.SHARED
    assert "friend@x.com" in c["reason"]
    assert not sharing.is_movable_by_default(f)


def test_not_owned_file_is_blocked_by_default():
    f = sole_file()
    f["owned_by_me"] = False
    f["owner_emails"] = ["boss@x.com"]
    c = sharing.classify(f)
    assert c["level"] == sharing.NOT_OWNED
    assert "boss@x.com" in c["reason"]
    assert "only remove it from your Drive" in c["reason"]
    assert not sharing.is_movable_by_default(f)


def test_unknown_metadata_warns_but_stays_movable():
    # Old scans carry no sharing fields: warn loudly, but don't break
    # existing flows by blocking everything.
    f = {"id": "b", "name": "old.mp4", "size": 50}
    assert sharing.classify(f)["level"] == sharing.UNKNOWN
    assert sharing.is_movable_by_default(f)
    warns = sharing.warnings([f])
    assert len(warns) == 1 and "rescan" in warns[0]["reason"]


def test_partition_splits_blocked_from_movable():
    ok_f = sole_file()
    shared_f = dict(ok_f, id="s", shared=True,
                    owner_emails=["me@x.com", "pal@x.com"])
    owned_f = dict(ok_f, id="o", owned_by_me=False,
                   owner_emails=["boss@x.com"])
    unknown_f = {"id": "u", "name": "old.mp4", "size": 1}
    ok, blocked = sharing.partition([ok_f, shared_f, owned_f, unknown_f])
    assert {f["id"] for f in ok} == {"a", "u"}
    assert {f["id"] for f in blocked} == {"s", "o"}

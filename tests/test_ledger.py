"""Ledger tests — use a temp HOME so the real ~/.spillover is untouched."""

import os
import sys
import tempfile
from pathlib import Path

_tmp = tempfile.mkdtemp()
os.environ["HOME"] = _tmp  # must precede importing spillover.config

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from spillover import ledger  # noqa: E402


def test_record_and_search():
    ledger.record_move(
        filename="vacation.mp4", size_bytes=1234, sha256="abc",
        src_alias="main", src_file_id="x1",
        dst_alias="alt1", dst_file_id="y1", status="done",
    )
    rows = ledger.search("vacation")
    assert len(rows) == 1, rows
    r = rows[0]
    assert r["src_alias"] == "main" and r["dst_alias"] == "alt1"
    assert r["status"] == "done"


def test_search_miss():
    assert ledger.search("definitely-not-a-file-xyz") == []


if __name__ == "__main__":
    test_record_and_search()
    test_search_miss()
    print("ledger tests passed")

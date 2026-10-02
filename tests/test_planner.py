"""Unit tests — no Google credentials needed."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from spillover.planner import Destination, plan_moves  # noqa: E402


def _files():
    return [
        {"id": "a", "name": "big.mp4", "size": 8_000_000_000},
        {"id": "b", "name": "mid.mp4", "size": 3_000_000_000},
        {"id": "c", "name": "small.jpg", "size": 500_000_000},
        {"id": "d", "name": "huge.iso", "size": 20_000_000_000},
    ]


def test_plan_respects_buffer():
    dests = [Destination("alt1", 10_000_000_000)]
    assignments, unplaced = plan_moves(
        _files(), dests, buffer_bytes=1_000_000_000
    )
    # usable = 9GB: big(8G) + small(0.5G) fit; mid(3G) and huge(20G) don't.
    placed_names = {a.file["name"] for a in assignments}
    assert placed_names == {"big.mp4", "small.jpg"}, placed_names
    assert {f["name"] for f in unplaced} == {"mid.mp4", "huge.iso"}


def test_plan_spreads_across_destinations():
    dests = [Destination("alt1", 10_000_000_000), Destination("alt2", 10_000_000_000)]
    files = [
        {"id": "a", "name": "f1", "size": 4_000_000_000},
        {"id": "b", "name": "f2", "size": 4_000_000_000},
    ]
    assignments, unplaced = plan_moves(files, dests, buffer_bytes=0)
    assert not unplaced
    dest_of = {a.file["name"]: a.dest_alias for a in assignments}
    # Worst-fit should put them on different accounts.
    assert dest_of["f1"] != dest_of["f2"], dest_of


def test_nothing_placed_when_no_space():
    dests = [Destination("alt1", 100_000_000)]  # 100MB < smallest file (500MB)
    assignments, unplaced = plan_moves(_files(), dests, buffer_bytes=0)
    assert assignments == []
    assert len(unplaced) == 4


if __name__ == "__main__":
    test_plan_respects_buffer()
    test_plan_spreads_across_destinations()
    test_nothing_placed_when_no_space()
    print("planner tests passed")

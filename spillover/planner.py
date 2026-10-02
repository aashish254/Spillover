"""Decide which files move to which destination account.

Greedy largest-first bin packing: every file lands on the destination with
the most remaining free space that can still fit it, always keeping the
safety buffer untouched.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .config import SAFETY_BUFFER_BYTES


@dataclass
class Destination:
    alias: str
    free_bytes: int
    usable: int = field(init=False)

    def __post_init__(self):
        self.usable = max(self.free_bytes - SAFETY_BUFFER_BYTES, 0)


@dataclass
class Assignment:
    file: dict
    dest_alias: str


def plan_moves(
    files: list[dict],
    destinations: list[Destination],
    buffer_bytes: int = SAFETY_BUFFER_BYTES,
) -> tuple[list[Assignment], list[dict]]:
    """Returns (assignments, unplaced_files). Never mutates inputs."""
    dests = [
        Destination(d.alias, d.free_bytes) for d in destinations
    ]
    for d in dests:
        d.usable = max(d.free_bytes - buffer_bytes, 0)

    assignments: list[Assignment] = []
    unplaced: list[dict] = []
    for f in sorted(files, key=lambda x: x["size"], reverse=True):
        # Worst-fit: the emptiest destination that fits, so data spreads
        # evenly across accounts instead of piling onto one.
        candidates = [d for d in dests if d.usable >= f["size"]]
        if not candidates:
            unplaced.append(f)
            continue
        best = max(candidates, key=lambda d: d.usable)
        assignments.append(Assignment(file=f, dest_alias=best.alias))
        best.usable -= f["size"]
    return assignments, unplaced

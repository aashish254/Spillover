"""Shared-file detection.

Moving a file that other people can access (or that you don't own) has
consequences a plain quota move doesn't: share links break, collaborators
lose access, and trashing a file you don't own only removes it from *your*
Drive — the original stays with its owner. This module classifies scan
entries so the planner, transfer engine, CLI and web UI can warn about them
and skip them by default.
"""

from __future__ import annotations

SOLE = "sole"            # only you: safe to move
SHARED = "shared"        # you own it, but others have access: warn loudly
NOT_OWNED = "not_owned"  # someone else owns it: moving is meaningless/dangerous
UNKNOWN = "unknown"      # scan predates sharing metadata: rescan to find out

_NON_SOLE = (SHARED, NOT_OWNED, UNKNOWN)


def classify(f: dict) -> dict:
    """Classify a scan entry dict.

    Returns {"level", "reason", "shared_with"} where level is one of
    "sole" | "shared" | "not_owned" | "unknown".
    """
    has_meta = (
        "shared" in f or "owned_by_me" in f or "owner_emails" in f
    )
    if not has_meta:
        return {
            "level": UNKNOWN,
            "reason": "sharing info unavailable — rescan the source account",
            "shared_with": [],
        }
    owner_emails = [e for e in (f.get("owner_emails") or []) if e]
    owned_by_me = f.get("owned_by_me", True)
    shared = bool(f.get("shared"))

    if not owned_by_me:
        who = ", ".join(owner_emails) if owner_emails else "someone else"
        return {
            "level": NOT_OWNED,
            "reason": (
                f"Owned by {who} — moving it would only remove it from your "
                "Drive; trashing can't delete the owner's original"
            ),
            "shared_with": owner_emails,
        }
    if shared or len(owner_emails) > 1:
        who = ", ".join(owner_emails) if owner_emails else "other people"
        return {
            "level": SHARED,
            "reason": (
                f"Shared with {who} — collaborators lose access when the "
                "original is trashed and existing share links break"
            ),
            "shared_with": owner_emails,
        }
    return {"level": SOLE, "reason": "", "shared_with": []}


def is_movable_by_default(f: dict) -> bool:
    """True when a file may move without an explicit user override.

    Shared or not-owned files are blocked by default. Files from old scans
    (no sharing metadata) are movable — but they show up in ``warnings()``
    with a rescan hint, so an old scan never silently moves a shared file
    without the user at least seeing the gap.
    """
    return classify(f)["level"] not in (SHARED, NOT_OWNED)


def partition(files: list[dict]) -> tuple[list[dict], list[dict]]:
    """Split files into (movable_by_default, needs_explicit_confirmation)."""
    ok, flagged = [], []
    for f in files:
        (ok if is_movable_by_default(f) else flagged).append(f)
    return ok, flagged


def warnings(files: list[dict]) -> list[dict]:
    """Every non-sole-owned file (shared, not-owned, or unknown) as a warning.

    ``unknown`` entries carry the rescan hint so old scans don't silently
    pass shared files through.
    """
    return [describe(f) for f in files if classify(f)["level"] != SOLE]


def describe(f: dict) -> dict:
    """Small JSON-friendly summary of a file's sharing state for the UI."""
    c = classify(f)
    return {
        "id": f.get("id"),
        "name": f.get("name"),
        "size": f.get("size", 0),
        "level": c["level"],
        "reason": c["reason"],
        "shared_with": c["shared_with"],
    }

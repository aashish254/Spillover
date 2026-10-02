"""Local SQLite ledger: the permanent record of what moved where.

This is the file that answers "where is my stuff?" — it never leaves the PC.
"""

from __future__ import annotations

import sqlite3
import time

from .config import LEDGER_DB, ensure_dirs

_SCHEMA = """
CREATE TABLE IF NOT EXISTS moves (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    filename TEXT NOT NULL,
    size_bytes INTEGER NOT NULL,
    sha256 TEXT,
    src_alias TEXT NOT NULL,
    src_file_id TEXT,
    dst_alias TEXT NOT NULL,
    dst_file_id TEXT,
    moved_at INTEGER NOT NULL,
    status TEXT NOT NULL,          -- done | failed | skipped
    note TEXT
);
CREATE INDEX IF NOT EXISTS idx_moves_filename ON moves(filename);
CREATE INDEX IF NOT EXISTS idx_moves_src ON moves(src_alias);
CREATE INDEX IF NOT EXISTS idx_moves_dst ON moves(dst_alias);

-- Per-file transfer transactions for crash resume.
-- state: pending -> in_progress -> done | failed
-- stage: last completed pipeline stage:
--        downloaded -> uploaded -> verified -> trashed
-- A transfer left in pending/in_progress when no run is active was
-- interrupted (crash, kill, power loss) and is eligible for resume.
CREATE TABLE IF NOT EXISTS transfers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    filename TEXT NOT NULL,
    size_bytes INTEGER NOT NULL,
    sha256 TEXT,
    mime_type TEXT,
    src_alias TEXT NOT NULL,
    src_file_id TEXT,
    dst_alias TEXT NOT NULL,
    dst_file_id TEXT,
    run_id TEXT NOT NULL,
    state TEXT NOT NULL,          -- pending | in_progress | done | failed
    stage TEXT,                   -- downloaded | uploaded | verified | trashed
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL,
    note TEXT
);
CREATE INDEX IF NOT EXISTS idx_transfers_state ON transfers(state);
CREATE INDEX IF NOT EXISTS idx_transfers_src_file ON transfers(src_file_id);
"""


def _connect() -> sqlite3.Connection:
    ensure_dirs()
    conn = sqlite3.connect(LEDGER_DB)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(_SCHEMA)
    return conn


def record_move(
    filename: str,
    size_bytes: int,
    sha256: str | None,
    src_alias: str,
    src_file_id: str | None,
    dst_alias: str,
    dst_file_id: str | None,
    status: str,
    note: str = "",
) -> int:
    with _connect() as conn:
        cur = conn.execute(
            """INSERT INTO moves
               (filename, size_bytes, sha256, src_alias, src_file_id,
                dst_alias, dst_file_id, moved_at, status, note)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (
                filename, size_bytes, sha256, src_alias, src_file_id,
                dst_alias, dst_file_id, int(time.time()), status, note,
            ),
        )
        return cur.lastrowid


def search(query: str, limit: int = 50) -> list[dict]:
    with _connect() as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """SELECT filename, size_bytes, sha256, src_alias, dst_alias,
                      dst_file_id, status, note,
                      datetime(moved_at, 'unixepoch', 'localtime') AS moved_at
               FROM moves WHERE filename LIKE ? ORDER BY id DESC LIMIT ?""",
            (f"%{query}%", limit),
        ).fetchall()
        return [dict(r) for r in rows]


def recent(limit: int = 20) -> list[dict]:
    with _connect() as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """SELECT filename, size_bytes, sha256, src_alias, dst_alias,
                      dst_file_id, status, note,
                      datetime(moved_at, 'unixepoch', 'localtime') AS moved_at
               FROM moves ORDER BY id DESC LIMIT ?""",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Transfer transactions (crash resume)
# ---------------------------------------------------------------------------

_PENDING_STATES = ("pending", "in_progress")
_DONE_STAGES = ("uploaded", "verified", "trashed")


def begin_transfer(
    filename: str,
    size_bytes: int,
    sha256: str | None,
    mime_type: str | None,
    src_alias: str,
    src_file_id: str | None,
    dst_alias: str,
    run_id: str,
) -> int:
    """Open a transaction for one file move. Returns the transfer id."""
    now = int(time.time())
    with _connect() as conn:
        cur = conn.execute(
            """INSERT INTO transfers
               (filename, size_bytes, sha256, mime_type, src_alias,
                src_file_id, dst_alias, dst_file_id, run_id,
                state, stage, created_at, updated_at, note)
               VALUES (?,?,?,?,?,?,?,?,?,'pending',NULL,?,?,?)""",
            (
                filename, size_bytes, sha256, mime_type, src_alias,
                src_file_id, dst_alias, None, run_id, now, now, "",
            ),
        )
        return cur.lastrowid


def update_transfer(
    transfer_id: int,
    state: str | None = None,
    stage: str | None = None,
    dst_file_id: str | None = None,
    sha256: str | None = None,
    run_id: str | None = None,
    note: str | None = None,
) -> None:
    """Advance a transaction. Only the given fields are updated."""
    sets, vals = [], []
    for col, val in (
        ("state", state), ("stage", stage), ("dst_file_id", dst_file_id),
        ("sha256", sha256), ("run_id", run_id), ("note", note),
    ):
        if val is not None:
            sets.append(f"{col} = ?")
            vals.append(val)
    if not sets:
        return
    sets.append("updated_at = ?")
    vals.append(int(time.time()))
    vals.append(transfer_id)
    with _connect() as conn:
        conn.execute(
            f"UPDATE transfers SET {', '.join(sets)} WHERE id = ?", vals
        )


def get_transfer(transfer_id: int) -> dict | None:
    with _connect() as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT * FROM transfers WHERE id = ?", (transfer_id,)
        ).fetchone()
        return dict(row) if row else None


def find_interrupted() -> list[dict]:
    """Transfers left unfinished by a previous run — resume candidates."""
    with _connect() as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """SELECT * FROM transfers WHERE state IN ('pending','in_progress')
               ORDER BY id"""
        ).fetchall()
        return [dict(r) for r in rows]


def find_done_transfer(src_file_id: str | None, dst_alias: str) -> dict | None:
    """An already-completed transfer for this source file/destination.

    Resume uses this as a guard so a finished file is never re-uploaded.
    """
    if not src_file_id:
        return None
    with _connect() as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            """SELECT * FROM transfers
               WHERE src_file_id = ? AND dst_alias = ? AND state = 'done'
               ORDER BY id DESC LIMIT 1""",
            (src_file_id, dst_alias),
        ).fetchone()
        return dict(row) if row else None


def transfer_stats() -> dict:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT state, COUNT(*) AS n FROM transfers GROUP BY state"
        ).fetchall()
        return {r[0]: r[1] for r in rows}

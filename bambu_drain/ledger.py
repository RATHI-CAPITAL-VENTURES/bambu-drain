"""The record of what has been taken off the stick and where it went.

Every delete in this project is gated on a row in here. A drain pass that
crashes between "copied" and "deleted" must be safe to re-run, and a ship pass
that crashes between "uploaded" and "verified" must not orphan a file on the
Pi — so both transitions are recorded separately and both are idempotent.
"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS files (
    sha256       TEXT PRIMARY KEY,
    src_name     TEXT NOT NULL,
    dest_rel     TEXT NOT NULL,
    size         INTEGER NOT NULL,
    staging_path TEXT,
    drained_at   REAL NOT NULL,
    shipped_at   REAL,
    verified_at  REAL
);
CREATE INDEX IF NOT EXISTS files_unshipped ON files (shipped_at) WHERE shipped_at IS NULL;

CREATE TABLE IF NOT EXISTS events (
    ts     REAL NOT NULL,
    kind   TEXT NOT NULL,
    detail TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS events_ts ON events (ts);
"""


class Ledger:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(path), isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        # WAL defaults to synchronous=NORMAL, which does not fsync on commit.
        # This ledger gates every delete from the printer's stick; a commit lost
        # to a power cut orphans a staged file that nothing will ever ship.
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.executescript(SCHEMA)

    def close(self) -> None:
        self.db.close()

    # -- drain side --------------------------------------------------------

    def known(self, sha: str) -> bool:
        cur = self.db.execute("SELECT 1 FROM files WHERE sha256 = ?", (sha,))
        return cur.fetchone() is not None

    def record_drained(
        self, sha: str, src_name: str, dest_rel: str, size: int, staging_path: Path
    ) -> None:
        self.db.execute(
            "INSERT OR REPLACE INTO files "
            "(sha256, src_name, dest_rel, size, staging_path, drained_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (sha, src_name, dest_rel, size, str(staging_path), time.time()),
        )

    # -- ship side ---------------------------------------------------------

    def unshipped(self) -> list[sqlite3.Row]:
        return list(
            self.db.execute(
                "SELECT * FROM files WHERE shipped_at IS NULL ORDER BY drained_at"
            )
        )

    def record_shipped(self, sha: str, verified: bool) -> None:
        now = time.time()
        self.db.execute(
            "UPDATE files SET shipped_at = ?, verified_at = ? WHERE sha256 = ?",
            (now, now if verified else None, sha),
        )

    def clear_staging(self, sha: str) -> None:
        self.db.execute("UPDATE files SET staging_path = NULL WHERE sha256 = ?", (sha,))

    # -- reporting ---------------------------------------------------------

    def event(self, kind: str, detail: str = "") -> None:
        self.db.execute(
            "INSERT INTO events (ts, kind, detail) VALUES (?, ?, ?)",
            (time.time(), kind, detail),
        )

    def stats(self) -> dict:
        row = self.db.execute(
            "SELECT COUNT(*) n, COALESCE(SUM(size), 0) bytes FROM files"
        ).fetchone()
        pending = self.db.execute(
            "SELECT COUNT(*) n, COALESCE(SUM(size), 0) bytes "
            "FROM files WHERE shipped_at IS NULL"
        ).fetchone()
        return {
            "files_total": row["n"],
            "bytes_total": row["bytes"],
            "files_pending_ship": pending["n"],
            "bytes_pending_ship": pending["bytes"],
        }

    def recent_events(self, limit: int = 20) -> list[sqlite3.Row]:
        return list(
            self.db.execute(
                "SELECT * FROM events ORDER BY ts DESC LIMIT ?", (limit,)
            )
        )

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
    session      TEXT,
    src_mtime    REAL,
    ends_session INTEGER DEFAULT 0,
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
        # Additive migration for ledgers created before print grouping.
        cols = {r[1] for r in self.db.execute("PRAGMA table_info(files)")}
        for col, typ in (("session", "TEXT"), ("src_mtime", "REAL"),
                         ("ends_session", "INTEGER DEFAULT 0")):
            if col not in cols:
                self.db.execute(f"ALTER TABLE files ADD COLUMN {col} {typ}")

    def close(self) -> None:
        self.db.close()

    # -- drain side --------------------------------------------------------

    def known(self, sha: str) -> bool:
        cur = self.db.execute("SELECT 1 FROM files WHERE sha256 = ?", (sha,))
        return cur.fetchone() is not None

    def record_drained(
        self, sha: str, src_name: str, dest_rel: str, size: int, staging_path: Path,
        session: str | None = None, src_mtime: float | None = None,
        ends_session: bool = False,
    ) -> None:
        self.db.execute(
            "INSERT OR REPLACE INTO files "
            "(sha256, src_name, dest_rel, size, staging_path, drained_at, "
            " session, src_mtime, ends_session) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (sha, src_name, dest_rel, size, str(staging_path), time.time(),
             session, src_mtime, 1 if ends_session else 0),
        )

    def sessions_needing_render(self) -> list[str]:
        """Closed sessions whose segments are still staged and have no timelapse.

        All three conditions matter. Closed, or we would render a print that is
        still running. Still staged, or there is nothing left on the Pi to render
        from. And no timelapse of either kind, or we would redo work — or worse,
        overwrite the printer's own.
        """
        rows = self.db.execute(
            "SELECT session FROM files WHERE session IS NOT NULL "
            "GROUP BY session HAVING "
            "  MAX(ends_session) = 1 "
            "  AND SUM(CASE WHEN staging_path IS NOT NULL THEN 1 ELSE 0 END) > 0 "
            "  AND SUM(CASE WHEN dest_rel LIKE '%timelapse%.mp4' THEN 1 ELSE 0 END) = 0"
        )
        return [r["session"] for r in rows]

    def staged_segments(self, session: str) -> list[Path]:
        """Staged chamber segments for a session, in order."""
        rows = self.db.execute(
            "SELECT staging_path FROM files WHERE session = ? "
            "AND staging_path IS NOT NULL AND src_name LIKE 'ipcam-record%.mp4' "
            "ORDER BY src_mtime", (session,))
        return [Path(r["staging_path"]) for r in rows]

    def session_dest_dir(self, session: str) -> str | None:
        """The archive-relative folder a session's files land in."""
        row = self.db.execute(
            "SELECT dest_rel FROM files WHERE session = ? LIMIT 1", (session,)
        ).fetchone()
        if not row:
            return None
        parts = row["dest_rel"].split("/")
        # prints/<session>/... -> prints/<session>
        return "/".join(parts[:2]) if len(parts) >= 2 else None

    def modal_size(self, like: str, minimum: int = 5) -> int | None:
        """The rotation size for a family of files, or None if not yet known.

        Uses the MEDIAN, not the mode. Real segments differ by a few kilobytes —
        240.2, 240.3, 240.4 MB — so no exact byte count ever repeats and a mode
        over raw sizes returns nothing. That is not a rounding nicety: the first
        version of this returned None for 61 perfectly good segments and
        silently disabled boundary detection entirely.

        The median is robust to the minority of short segments, which is exactly
        the population being measured against.
        """
        sizes = [r["size"] for r in self.db.execute(
            "SELECT size FROM files WHERE src_name LIKE ? ORDER BY size", (like,))]
        if len(sizes) < minimum:
            return None
        return sizes[len(sizes) // 2]

    def last_print_file(self) -> sqlite3.Row | None:
        """The most recent file assigned to a print session, by source mtime.

        Session continuity is decided against this: a new file within the gap
        joins it, anything later opens a new session. Ordering by src_mtime (the
        printer's clock for that file) rather than drained_at matters, because a
        backlog is drained long after the fact and all in one go.
        """
        cur = self.db.execute(
            "SELECT session, src_mtime, ends_session FROM files "
            "WHERE session IS NOT NULL AND src_mtime IS NOT NULL "
            # Ties broken so a session CLOSER sorts last: the truncated final
            # segment and the timelapse are flushed in the same second, and the
            # closer is by definition the end of the run.
            "ORDER BY src_mtime DESC, ends_session DESC LIMIT 1"
        )
        return cur.fetchone()

    # -- ship side ---------------------------------------------------------

    def unshipped(self, only_closed_sessions: bool = False,
                  max_hold_seconds: float = 6 * 3600) -> list[sqlite3.Row]:
        """Files not yet on the ship host.

        With `only_closed_sessions`, a session's files are held back until that
        session has ended. Shipping deletes the staged segments, and they are
        the raw material for rebuilding a missing timelapse — so shipping them
        mid-print destroys the ability to render one.

        This is not hypothetical: a 5.79 GB print took several drain passes, the
        ship loop cleared staging between them, and by the time the final short
        segment closed the session only one segment remained. Every print large
        enough to need more than one pass silently lost its timelapse.

        Files with no session (a sliced model, anything ungrouped) are never
        held.
        """
        if not only_closed_sessions:
            return list(self.db.execute(
                "SELECT * FROM files WHERE shipped_at IS NULL ORDER BY drained_at"))
        # A session that never closes must not wedge the pipeline. A printer
        # switched off mid-print, or a final segment that happens to come in
        # full-size, would otherwise hold its files — and every file behind
        # them — indefinitely, until staging filled and the drain loop stopped.
        # After max_hold_seconds of no new files, ship it anyway and accept
        # that it gets no rebuilt timelapse.
        cutoff = time.time() - max_hold_seconds
        return list(self.db.execute(
            "SELECT * FROM files WHERE shipped_at IS NULL AND ("
            "  session IS NULL OR session IN ("
            "    SELECT session FROM files WHERE session IS NOT NULL"
            "    GROUP BY session"
            "    HAVING MAX(ends_session) = 1"
            "        OR MAX(COALESCE(src_mtime, drained_at)) < ?))"
            " ORDER BY drained_at", (cutoff,)))

    def open_sessions(self, max_hold_seconds: float = 6 * 3600) -> list[str]:
        """Sessions still being held: not ended, and not yet timed out."""
        cutoff = time.time() - max_hold_seconds
        return [r["session"] for r in self.db.execute(
            "SELECT session FROM files WHERE session IS NOT NULL "
            "AND shipped_at IS NULL GROUP BY session "
            "HAVING MAX(ends_session) = 0 "
            "   AND MAX(COALESCE(src_mtime, drained_at)) >= ?", (cutoff,))]

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

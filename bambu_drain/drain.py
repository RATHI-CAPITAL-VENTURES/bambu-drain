"""Loop A — take files off the stick, fast.

This loop owns the only dangerous moment in the project: the window where the
medium is ejected and the Pi has the filesystem mounted. Everything here is
shaped to make that window short and bounded.

It deliberately does NOT talk to the Mac. Copying a 300 MB timelapse over Wi-Fi
would hold the printer's storage hostage for minutes; copying it to the Pi's own
disk takes seconds. The network hop is ship.py's problem, and it runs with the
media happily re-inserted.
"""

from __future__ import annotations

import logging
import os
import shutil
import time
from datetime import datetime, timezone
from pathlib import Path

from . import imagefs
from .gadget import MassStorageGadget
from .ledger import Ledger

log = logging.getLogger("bambu_drain.drain")


def dest_relpath(rule, src: Path, when: float) -> str:
    d = datetime.fromtimestamp(when, tz=timezone.utc)
    return f"{rule.dest}/{d:%Y}/{d:%m}/{src.name}"


def fsync_file_and_parent(path: Path) -> None:
    """Force a staged copy to durable storage before anything is deleted.

    This is the load-bearing line of the whole project, and it was missing.

    `shutil.copy2` leaves the data in the page cache. A read-back checksum then
    reads it *from that cache* and cheerfully confirms bytes that are not on the
    disk yet — so the verify passes, we delete the original off the stick, and a
    power loss in that window destroys the only durable copy.

    Found the hard way: a 150 MB file was drained, verified, deleted from the
    stick, and reduced to a 0-byte staging file by an unclean shutdown seconds
    later. ext4's delayed allocation had journalled the directory entry but
    never written the data. The file was gone.

    The parent directory needs its own fsync: the file's data being durable is
    no help if the directory entry pointing at it is not.
    """
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)

    dfd = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(dfd)
    finally:
        os.close(dfd)


def _unique(path: Path, sha: str) -> Path:
    """Two prints can produce the same filename on different days."""
    if not path.exists():
        return path
    return path.with_name(f"{path.stem}-{sha[:8]}{path.suffix}")


def staging_usage_bytes(staging: Path) -> int:
    if not staging.exists():
        return 0
    return sum(p.stat().st_size for p in staging.rglob("*") if p.is_file())


class Drainer:
    def __init__(self, cfg, ledger: Ledger, gadget: MassStorageGadget):
        self.cfg = cfg
        self.ledger = ledger
        self.gadget = gadget

    # -- gating ------------------------------------------------------------

    def blocked_reason(self) -> str | None:
        """Why we must not drain right now, or None if we may."""
        d = self.cfg.drain
        idle = self.gadget.idle_seconds()
        if idle < d.idle_minutes * 60:
            return f"printer active ({idle:.0f}s since last write)"

        used = staging_usage_bytes(d.staging)
        if used > d.staging_max_gb * 1024**3:
            # Draining now would trade the printer's full disk for the Pi's
            # full disk, which is a worse failure: the Pi is also the thing
            # that fixes it.
            return (
                f"staging over budget ({used / 1024**3:.1f} GB > "
                f"{d.staging_max_gb} GB) — ship loop is behind or the Mac is off"
            )
        return None

    # -- a pass ------------------------------------------------------------

    def run_once(self, dry_run: bool = False) -> dict:
        reason = self.blocked_reason()
        if reason:
            log.debug("skipping drain: %s", reason)
            return {"skipped": reason, "moved": 0, "bytes": 0}

        d = self.cfg.drain
        started = time.monotonic()
        moved = 0
        total = 0
        truncated = False

        self.gadget.cycle_out()
        try:
            with imagefs.mounted(self.cfg.gadget.image, d.mount_point, self.cfg.gadget.fs) as mp:
                for src, rule, st in imagefs.candidates(
                    mp, d.rules, d.min_file_age_minutes * 60
                ):
                    if time.monotonic() - started > d.max_eject_seconds:
                        # Hand the medium back and finish next pass. A printer
                        # with its stick back beats a complete drain.
                        truncated = True
                        log.warning("drain pass hit max_eject_seconds; deferring rest")
                        break

                    sha = imagefs.sha256(src)
                    if self.ledger.known(sha):
                        # Already safely off the stick in a previous pass that
                        # died before the delete. Re-delete and move on.
                        if rule.delete and not dry_run:
                            src.unlink()
                        continue

                    rel = dest_relpath(rule, src, st.st_mtime)
                    target = _unique(d.staging / rel, sha)

                    if dry_run:
                        log.info("[dry-run] %s -> %s (%d bytes)", src.name, rel, st.st_size)
                        moved += 1
                        total += st.st_size
                        continue

                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(src, target)

                    # Durability BEFORE verification, and both before the
                    # delete. Verifying a page-cached copy proves nothing about
                    # what survives a power cut.
                    fsync_file_and_parent(target)

                    # Verify before the delete. This is the whole contract.
                    if imagefs.sha256(target) != sha or target.stat().st_size != st.st_size:
                        target.unlink(missing_ok=True)
                        log.error("checksum mismatch copying %s — left on stick", src.name)
                        self.ledger.event("copy_mismatch", src.name)
                        continue

                    self.ledger.record_drained(
                        sha, src.name, str(target.relative_to(d.staging)), st.st_size, target
                    )
                    if rule.delete:
                        src.unlink()

                    moved += 1
                    total += st.st_size
                    log.info("drained %s (%.1f MB)", src.name, st.st_size / 1024**2)
        finally:
            # Unconditional. If the mount or the copy blew up, the printer still
            # gets its stick back.
            self.gadget.cycle_in()

        if moved:
            self.ledger.event("drain", f"{moved} files, {total} bytes")
        return {
            "skipped": None,
            "moved": moved,
            "bytes": total,
            "truncated": truncated,
            "seconds": time.monotonic() - started,
        }

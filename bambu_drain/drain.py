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
from .lock import AlreadyRunning, single_instance
from .ledger import Ledger

log = logging.getLogger("bambu_drain.drain")


def session_name(when: float) -> str:
    """A print session's folder name, from the first file that opened it."""
    return f"{datetime.fromtimestamp(when):%Y-%m-%d_%H%M}"


def dest_relpath(rule, src: Path, when: float, session: str | None = None) -> str:
    """Where a file lands in the archive.

    Per-print files go under `prints/<session>/<dest>/`, so everything from one
    run sits together — 22 video segments, their thumbnails and the assembled
    timelapse. Everything else keeps the dated layout, because a sliced model or
    a firmware image does not belong to a print.
    """
    name = rule.rename or src.name
    if rule.group == "print" and session:
        parts = ["prints", session] + ([rule.dest] if rule.dest else []) + [name]
        return "/".join(parts)
    d = datetime.fromtimestamp(when, tz=timezone.utc)
    return f"{rule.dest}/{d:%Y}/{d:%m}/{name}"


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


def _distinct(name: str, previous: str | None) -> str:
    """A session name that cannot collide with the one it follows."""
    if previous is None:
        return name
    # Compare against the previous name's BASE, not its suffixed form: after
    # "…_0927-2" exists, plain "…_0927" is still taken by the session that
    # forced the suffix in the first place.
    base, _, n = previous.rpartition("-")
    if base and n.isdigit():
        prev_base, prev_n = base, int(n)
    else:
        prev_base, prev_n = previous, 1
    if name != prev_base:
        return name
    return f"{name}-{prev_n + 1}"


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
        # OUR OWN deletions bump the backing image's mtime — the very signal we
        # use to detect the printer writing. So without this, every pass resets
        # its own idle clock, and draining a backlog too big for one window
        # means waiting the full idle gate again between each chunk. A 4-hour
        # print left 5.4 GB and took four passes over ~25 minutes for that
        # reason. We remember the mtime we caused and look through it.
        self._own_mtime: float | None = None
        self._printer_mtime: float | None = None

    # -- gating ------------------------------------------------------------

    def quiet_seconds(self) -> float:
        """How long since the PRINTER last wrote, ignoring our own writes."""
        try:
            current = self.gadget.last_host_write()
        except OSError:
            return 0.0
        if (self._own_mtime is not None
                and abs(current - self._own_mtime) < 0.001
                and self._printer_mtime is not None):
            # Nothing has touched the image since our own pass finished.
            current = self._printer_mtime
        return max(0.0, time.time() - current)

    def eject_budget(self) -> float:
        """How long we may hold the medium, scaled by confidence.

        Five minutes of quiet is the minimum bar for believing a print ended;
        twenty is near certainty. The budget follows that confidence, so the
        common case (a small drain right after a print) stays conservative
        while a large backlog does not get chopped into a dozen windows.
        """
        d = self.cfg.drain
        if self.quiet_seconds() >= d.long_idle_minutes * 60:
            return d.max_eject_seconds_long_idle
        return d.max_eject_seconds

    def blocked_reason(self) -> str | None:
        """Why we must not drain right now, or None if we may."""
        d = self.cfg.drain
        idle = self.quiet_seconds()
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

    def session_for(self, mtime: float) -> str:
        """Which print run a file belongs to, by gap from the previous one.

        There is no print id anywhere in the filenames, so this is a heuristic —
        see `session_gap_minutes`. It holds up because the measured separation is
        not close: 9-18 minutes within a print, 805 minutes between them.
        """
        gap = self.cfg.drain.session_gap_minutes * 60
        last = self.ledger.last_print_file()
        if last and last["src_mtime"] is not None:
            if not last["ends_session"] and abs(mtime - last["src_mtime"]) <= gap:
                return last["session"]
            # Either a timelapse closed that print, or the gap is too large.
            # Session names are minute-granular for readability, so a genuinely
            # new session starting inside the same minute would silently reuse
            # the previous folder. Rare, but a collision is a merged print.
            return _distinct(session_name(mtime), last["session"])
        return session_name(mtime)

    def _ensure_medium_present(self) -> None:
        try:
            present = self.gadget.media_present
        except (OSError, AttributeError):
            return  # gadget not created yet; the gadget service owns that
        if present:
            return
        log.warning(
            "medium was absent at the start of a pass — re-inserting. "
            "A previous pass died between eject and insert, and the printer "
            "has had no storage since."
        )
        self.ledger.event("medium_reinserted", "found absent at start of pass")
        self.gadget.cycle_in()

    # -- a pass ------------------------------------------------------------

    def run_once(self, dry_run: bool = False) -> dict:
        try:
            with single_instance(self.cfg.drain_lock_path):
                return self._run_once_locked(dry_run)
        except AlreadyRunning as exc:
            # Reported as a skip rather than raised: running `drain --once`
            # while the daemon is mid-pass is normal, not an error.
            log.info("%s", exc)
            return {"skipped": str(exc), "moved": 0, "bytes": 0}

    def _run_once_locked(self, dry_run: bool = False) -> dict:
        # Self-heal first, before any gate.
        #
        # If a pass ever dies between eject and insert — a crash, a SIGKILL, an
        # interrupted manual command — the printer is left looking at a card
        # reader with no card. It reports "no USB drive" and silently has
        # nowhere to write, which is indistinguishable from the cable falling
        # out and is exactly the failure this project exists to prevent.
        #
        # This is safe precisely here: we hold the drain lock, so no other pass
        # is legitimately mid-cycle with the medium out on purpose.
        self._ensure_medium_present()

        reason = self.blocked_reason()
        if reason:
            log.debug("skipping drain: %s", reason)
            return {"skipped": reason, "moved": 0, "bytes": 0}

        d = self.cfg.drain
        started = time.monotonic()
        budget = self.eject_budget()
        moved = 0
        total = 0
        truncated = False

        try:
            pre_mtime = self.cfg.gadget.image.stat().st_mtime
        except OSError:
            pre_mtime = time.time()

        self.gadget.cycle_out()
        try:
            with imagefs.mounted(self.cfg.gadget.image, d.mount_point, self.cfg.gadget.fs) as mp:
                # Sorted by mtime, not by path: session boundaries are decided
                # chronologically, and a backlog is drained all at once long
                # after the fact.
                found = sorted(
                    imagefs.candidates(mp, d.rules, d.min_file_age_minutes * 60),
                    # Closers last within the same second — see ledger ordering.
                    key=lambda t: (t[2].st_mtime, 1 if t[1].ends_session else 0),
                )
                for src, rule, st in found:
                    if time.monotonic() - started > budget:
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
                            src.unlink(missing_ok=True)
                        continue

                    session = self.session_for(st.st_mtime) if rule.group == "print" else None
                    rel = dest_relpath(rule, src, st.st_mtime, session)
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
                        sha, src.name, str(target.relative_to(d.staging)), st.st_size,
                        target, session=session, src_mtime=st.st_mtime,
                        ends_session=rule.ends_session,
                    )
                    if rule.delete:
                        src.unlink(missing_ok=True)

                    moved += 1
                    total += st.st_size
                    log.info("drained %s (%.1f MB)", src.name, st.st_size / 1024**2)
        finally:
            # Unconditional. If the mount or the copy blew up, the printer still
            # gets its stick back.
            self.gadget.cycle_in()
            # Record the mtime our own deletions caused, and what the printer's
            # was before we touched it, so the next pass does not mistake our
            # writes for the printer waking up.
            try:
                self._printer_mtime = pre_mtime
                self._own_mtime = self.cfg.gadget.image.stat().st_mtime
            except OSError:
                self._own_mtime = None

        if moved:
            self.ledger.event("drain", f"{moved} files, {total} bytes")
        return {
            "skipped": None,
            "moved": moved,
            "bytes": total,
            "truncated": truncated,
            "budget_seconds": budget,
            "seconds": time.monotonic() - started,
        }

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
import re
import shutil
import time
from datetime import datetime, timezone
from pathlib import Path

from . import imagefs
from .gadget import MassStorageGadget
from .lock import AlreadyRunning, single_instance
from .ledger import Ledger

log = logging.getLogger("bambu_drain.drain")


# Bambu Studio names the sent file after the PROCESS PRESET whenever the project
# itself is unnamed, so roughly half of them are "0.2mm layer, 2 walls, 15%
# infill" rather than anything about the model. Slicing also strips the mesh
# objects, so there is no better name inside the file — this is the only signal
# there is, and it has to be filtered.
_PRESET_MARKERS = ("infill", "walls", "mm layer", "mm nozzle", "layer,",
                   "@bbl", "@bambu")
# Bambu's own presets are overwhelmingly "<layer height>mm <quality> @<printer>"
# — "0.16mm Optimal @BBL X1C", "0.2mm Standard". A name that opens with a layer
# height is a preset, not a model.
_PRESET_PREFIX = re.compile(r"^\d+(\.\d+)?\s*mm\b", re.I)


def model_name(src: Path) -> str | None:
    """A usable model name from a sliced file, or None if it is a preset name."""
    name = src.name
    for suffix in (".gcode.3mf", ".3mf", ".gcode"):
        if name.lower().endswith(suffix):
            name = name[: -len(suffix)]
            break
    name = name.strip()
    if not name:
        return None
    low = name.lower()
    if any(m in low for m in _PRESET_MARKERS) or _PRESET_PREFIX.match(name):
        return None
    slug = re.sub(r"[^\w.-]+", "_", name).strip("_.")
    return slug[:60] or None


def session_name(when: float, model: str | None = None) -> str:
    """A print session's folder name, from the file that opened it.

    `<model>_MM_DD_YY`, or a bare `MM_DD_YY` when the sliced file carried a
    preset name rather than a model's. Model first, because Finder is scanned
    by name; short date second, because it is what you check once the name
    matches. No time of day: it prefixed every folder with fourteen characters
    nobody read, and a same-day repeat is told apart by `_distinct` instead.
    """
    stamp = f"{datetime.fromtimestamp(when):%m_%d_%y}"
    return f"{model}_{stamp}" if model else stamp


def dest_relpath(rule, src: Path, when: float, session: str | None = None,
                 size: int | None = None) -> str:
    """Where a file lands in the archive.

    Per-print files go under `prints/<session>/<dest>/`, so everything from one
    run sits together — 22 video segments, their thumbnails and the assembled
    timelapse. Everything else keeps the dated layout, because a sliced model or
    a firmware image does not belong to a print.
    """
    # An empty file does not get the canonical name. The printer exported a
    # 0-byte timelapse alongside a real 15.9 MB one; sorted first, the empty one
    # took `timelapse.mp4` and the real one was pushed to a hash-suffixed name.
    # Keeping its own filename makes it obviously not the timelapse.
    name = rule.rename if (rule.rename and size != 0) else src.name
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


# A print's teardown is one flush. Measured on the real printer, in order and
# to the hundredth of a second: the timelapse's thumbnail, the short final
# segment, the timelapse's `_mini` thumbnail, the timelapse itself — 0.18 s
# from first to last, and the two closers 0.08 s apart. Anything landing this
# soon after a closer is part of that flush and joins the print it closed.
#
# Declared in 0.6.0 at 120 s and applied only in the archive migration — the
# daemon never read it, and every print with a timelapse got a one-file folder
# for it (`2026-09-16_0024/timelapse.mp4` beside a 4.4-hour print with none).
# It is 30 s now, not 120, because the window is no longer closers-only: the
# closest redo on record started 141 s after the short segment that ended the
# failed attempt, and its first file is a thumbnail, not a closer.
TEARDOWN_SECONDS = 30

# The sliced file "lands ~15 minutes before the first segment" held for the
# print that motivated `starts_session` and not for the next two, where the
# chamber recording's first thumbnail was written 1-2 s BEFORE the .gcode.3mf.
# Sorted by mtime, that thumbnail opened an unnamed session, the sliced file
# opened the named one a second later, and the thumbnail sat alone for six
# hours before shipping as a one-file folder. A nameless session that opened
# this recently before a sliced file landed is that print's own recording, and
# the sliced file takes it over — records, staged files and all.
START_SKEW_SECONDS = 120

_UNNAMED = re.compile(r"^\d{2}_\d{2}_\d{2}(-\d+)?$")


def is_unnamed(session: str) -> bool:
    """A session folder that carries only its timestamp — no model name."""
    return bool(_UNNAMED.match(session))


def reroot(dest_rel: str, old: str, new: str) -> str:
    """`prints/<old>/…` -> `prints/<new>/…`; anything else unchanged."""
    parts = dest_rel.split("/")
    if len(parts) >= 2 and parts[0] == "prints" and parts[1] == old:
        parts[1] = new
    return "/".join(parts)


def _size_family(src: Path) -> str:
    """A SQL LIKE pattern matching files that rotate at the same size.

    `ipcam-record.<ts>.<n>.mp4` -> `ipcam-record%.mp4`, so the modal size is
    computed across the whole family rather than per print.
    """
    stem = src.name.split(".")[0]
    return f"{stem}%{src.suffix}"


def _distinct(name: str, taken) -> str:
    """A session name that collides with none already in use.

    Checked against EVERY session ever named, not just the one before it.
    Names are day-granular, so printing A, then B, then A again in one
    afternoon is ordinary — and against only its predecessor the second A
    would have merged into the first, with B sitting in between.
    """
    taken = set(taken or ())
    if name not in taken:
        return name
    n = 2
    while f"{name}-{n}" in taken:
        n += 1
    return f"{name}-{n}"


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

    def closes_session(self, rule, src: Path, size: int) -> bool:
        """Does this file end a print?

        Either the rule says so outright (the timelapse, written once at the
        end), or the file is a rotating segment that came in short — which means
        the recording was cut off, which means the print stopped.
        """
        if rule.ends_session:
            return True
        if not rule.ends_session_if_short:
            return False
        modal = self.ledger.modal_size(_size_family(src))
        if not modal:
            return False          # not enough history to know what "full" is
        return size < modal * rule.ends_session_if_short

    def session_for(self, mtime: float, rule=None, src: Path | None = None,
                    is_closer: bool = False) -> str:
        """Which print run a file belongs to, by gap from the previous one.

        There is no print id anywhere in the filenames, so this is a heuristic —
        see `session_gap_minutes`. It holds up because the measured separation is
        not close: 9-18 minutes within a print, 805 minutes between them.
        """
        model = model_name(src) if (rule and rule.names_session and src) else None
        last = self.ledger.last_print_file()
        taken = self.ledger.session_names()

        if rule and rule.starts_session:
            # The sliced file means a job was just sent: a new print, always —
            # unless its own recording beat it to the stick by a second, in
            # which case that nameless session IS this print, and takes the name.
            name = session_name(mtime, model)
            if last and self._opened_just_before(last, mtime):
                # The nameless session is free to take this name; every OTHER
                # session is not — a second print of the same model that day
                # must not be moved into the first one's folder.
                name = _distinct(name, taken - {last["session"]})
                if name != last["session"]:
                    self._rename_session(last["session"], name)
                return name
            return _distinct(name, taken)

        closer = self.ledger.last_closer()
        if (closer and last and last["session"] == closer["session"]
                and 0 <= mtime - closer["src_mtime"] <= TEARDOWN_SECONDS):
            # Part of the flush that ended the print: the timelapse and its
            # thumbnails arrive within a fraction of a second of the short
            # final segment, and whichever sorts first must not strand the rest.
            return closer["session"]

        gap = self.cfg.drain.session_gap_minutes * 60
        if last and last["src_mtime"] is not None:
            if not last["ends_session"] and abs(mtime - last["src_mtime"]) <= gap:
                return last["session"]
            # Either a timelapse closed that print, or the gap is too large.
            # Session names are day-granular, so a redo the same afternoon
            # would silently reuse the folder — and a collision is a merged
            # print. `_distinct` suffixes it instead.
            return _distinct(session_name(mtime, model), taken)
        return _distinct(session_name(mtime, model), taken)

    def _opened_just_before(self, last, mtime: float) -> bool:
        """Is `last`'s session a nameless recording that began just before a
        sliced file landed — i.e. that sliced file's own print?"""
        if last["ends_session"] or last["src_mtime"] is None:
            return False
        if not is_unnamed(last["session"]):
            return False
        opened = self.ledger.session_opened_at(last["session"])
        return opened is not None and 0 <= mtime - opened <= START_SKEW_SECONDS

    def _rename_session(self, old: str, new: str) -> None:
        """Re-home a session's records and staged files under a new name.

        Per file: move on disk, then update the row, so a crash mid-way leaves
        each row pointing at wherever its file actually is. Already-shipped
        rows only change name — their copy on the Mac is out of reach here.
        """
        staging = self.cfg.drain.staging
        for row in self.ledger.session_files(old):
            rel = reroot(row["dest_rel"], old, new)
            staged = None
            if row["staging_path"]:
                src = Path(row["staging_path"])
                dst = staging / rel
                if src.exists() and not dst.exists():
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    src.rename(dst)
                    staged = dst
                else:
                    staged = src
            self.ledger.reassign(row["sha256"], new, rel, staged)
        old_dir = staging / "prints" / old
        for d in sorted((p for p in old_dir.rglob("*") if p.is_dir()),
                        key=lambda p: len(p.parts), reverse=True):
            try:
                d.rmdir()
            except OSError:
                pass
        try:
            old_dir.rmdir()
        except OSError:
            pass
        log.info("session %s is %s — its recording started before the sliced "
                 "file landed", old, new)
        self.ledger.event("session_renamed", f"{old} -> {new}")

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
                    key=lambda t: (t[2].st_mtime,
                                   -1 if t[1].starts_session else
                                   (1 if self.closes_session(t[1], t[0], t[2].st_size) else 0)),
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

                    ends = self.closes_session(rule, src, st.st_size)
                    session = (self.session_for(st.st_mtime, rule, src, ends)
                               if rule.group == "print" else None)
                    rel = dest_relpath(rule, src, st.st_mtime, session,
                                       size=st.st_size)
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
                        ends_session=ends,
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

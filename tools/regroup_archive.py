#!/usr/bin/env python3
"""Reorganise an existing archive into one folder per print.

Run on the machine holding the archive (the ship host), not the Pi.

    tools/regroup_archive.py --src <old> [--dest <new>]        # dry run
    tools/regroup_archive.py --src <old> --dest <new> --apply

Sessions are inferred from file mtimes, exactly as the drain loop does it: files
more than `--gap` minutes apart belong to different prints. That works because
the measured separation is not close — 9-18 minutes within a print against 805
between them — but it IS a heuristic, and the dry run exists so you can check
its verdict against what you remember printing before anything moves.

Nothing is deleted. Files are MOVED, and an existing destination is never
overwritten; a collision is reported and skipped.
"""

from __future__ import annotations

import argparse
import datetime as dt
import os
import re
import shutil
import sys
from pathlib import Path

VIDEO_EXT = {".mp4", ".avi"}
THUMB_EXT = {".jpg", ".jpeg", ".png"}
MODEL_EXT = {".3mf", ".gcode"}

# Shared with the drain loop rather than reimplemented. The first version of
# this file duplicated both the model-name rules AND the modal-size calculation,
# and the duplicate kept the exact-value mode long after the real one moved to a
# median — so boundary detection here was silently disabled while the daemon's
# worked fine. Two copies of a rule is how they drift.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from bambu_drain.drain import model_name  # noqa: E402


# Files this project GENERATES, which must never be mistaken for printer output.
# `timelapse-reconstructed.mp4` is a 169 MB .mp4 sitting in a print folder; read
# as a chamber segment it came in at 70% of the rotation size, which reads as a
# print ending and split one print into two. A tool that eats its own output
# produces different results on the second run than the first.
_GENERATED = {"timelapse-reconstructed.mp4"}


def classify(path: Path) -> tuple[str, str] | None:
    """(kind, subfolder) for a file, or None to leave it alone."""
    ext = path.suffix.lower()
    name = path.name
    if name in _GENERATED:
        return None
    if ext in VIDEO_EXT:
        # The assembled timelapse is one per print and belongs at the root of
        # its folder; the chamber segments go in video/.
        #
        # Both spellings matter: `video_<ts>.mp4` is what the printer writes,
        # and `timelapse.mp4` is what a previous run of THIS script renamed it
        # to. Re-running must still recognise it, or the session-closing signal
        # is destroyed by the first migration.
        if name.startswith("video_") or name == "timelapse.mp4":
            return ("timelapse", "")
        return ("video", "video")
    if ext in THUMB_EXT:
        return ("thumb", "thumbnails")
    if ext in MODEL_EXT:
        # The sliced file belongs to its print and starts it.
        return ("sliced", "")
    return None


def modal_size(files: list[Path]) -> int | None:
    """The rotation size of the chamber recording, learned from the files.

    MEDIAN, not mode — real segments differ by a few kilobytes, so no exact
    size ever repeats and a mode returns nothing. Same reasoning, and the same
    bug once, as `Ledger.modal_size`.
    """
    sizes = sorted(f.stat().st_size for f in files
                   if classify(f) and classify(f)[0] == "video")
    if len(sizes) < 5:
        return None
    return sizes[len(sizes) // 2]


def sessions(files: list[Path], gap_seconds: float, short_ratio: float = 0.95
             ) -> dict[Path, str]:
    """Group print files into sessions, chronologically.

    Two things end a session, in order of reliability:

    1. **A short chamber segment.** The recording rotates at a fixed size, so a
       segment that comes in under `short_ratio` of the modal size was closed
       early — meaning the print stopped. Physical, not inferential.
    2. **The timelapse**, written once when a print ends — when it reaches the
       drive at all, which it often does not.

    The time gap is only a last resort. A real boundary measured 18.2 minutes
    while gaps within a print reach 14, so no threshold separates them without
    risking fragmenting a long print.
    """
    out: dict[Path, str] = {}
    current: str | None = None
    last: float | None = None
    closed = False
    modal = modal_size(files)
    # See TEARDOWN_SECONDS in bambu_drain.drain — a print's last segment and its
    # timelapse are flushed together and both end the session.
    TEARDOWN = 120

    def starts(p: Path) -> bool:
        c = classify(p)
        return bool(c and c[0] == "sliced")

    def ends(p: Path) -> bool:
        c = classify(p)
        if c and c[0] == "timelapse":
            return True
        if modal and c and c[0] == "video":
            return p.stat().st_size < modal * short_ratio
        return False

    # Ties: the truncated final segment and the timelapse land in the same
    # second, and the closer is by definition the end of the run.
    def key(p: Path):
        # A starter sorts before everything sharing its second; a closer after.
        return (p.stat().st_mtime, -1 if starts(p) else (1 if ends(p) else 0))

    for f in sorted(files, key=key):
        m = f.stat().st_mtime
        teardown = closed and ends(f) and last is not None and (m - last) <= TEARDOWN
        if not teardown and (starts(f) or current is None or closed
                             or last is None or (m - last) > gap_seconds):
            stamp = f"{dt.datetime.fromtimestamp(m):%Y-%m-%d_%H%M}"
            mn = model_name(f) if starts(f) else None
            name = f"{stamp}_{mn}" if mn else stamp
            if name == current:
                base, _, n = (current or "").rpartition("-")
                name = f"{base}-{int(n) + 1}" if base and n.isdigit() else f"{name}-2"
            current = name
            closed = False
        out[f] = current
        last = m
        closed = ends(f)
    return out


def plan(src: Path, dest: Path, gap_seconds: float, short_ratio: float = 0.95):
    print_files, others, ignored = [], [], []
    for f in sorted(src.rglob("*")):
        if not f.is_file() or f.name.startswith("."):
            continue
        c = classify(f)
        if c is None:
            ignored.append(f)
        else:
            print_files.append(f)

    sess = sessions(print_files, gap_seconds, short_ratio)
    moves: list[tuple[Path, Path]] = []
    for f in print_files:
        kind, sub = classify(f)
        name = "timelapse.mp4" if kind == "timelapse" and f.stat().st_size else f.name
        parts = ["prints", sess[f]] + ([sub] if sub else []) + [name]
        moves.append((f, dest.joinpath(*parts)))
    return moves, sess, ignored


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--src", required=True, type=Path)
    ap.add_argument("--dest", type=Path, help="defaults to --src (in place)")
    ap.add_argument("--gap", type=float, default=45.0, help="session gap, minutes")
    ap.add_argument("--short", type=float, default=0.95,
                    help="fraction of the modal segment size below which a "
                         "segment is taken to end a print (default 0.95)")
    ap.add_argument("--apply", action="store_true", help="actually move files")
    args = ap.parse_args()

    src = args.src.expanduser()
    dest = (args.dest or args.src).expanduser()
    if not src.is_dir():
        print(f"error: {src} is not a directory", file=sys.stderr)
        return 1

    moves, sess, ignored = plan(src, dest, args.gap * 60, args.short)
    if not moves:
        print("nothing to move")
        return 0

    by_session: dict[str, list] = {}
    for f, t in moves:
        by_session.setdefault(sess.get(f, "models"), []).append((f, t))

    print(f"{'MOVING' if args.apply else 'DRY RUN — nothing will move'}")
    print(f"  from {src}")
    print(f"  to   {dest}\n")
    for s in sorted(by_session):
        rows = by_session[s]
        total = sum(f.stat().st_size for f, _ in rows)
        span = [f.stat().st_mtime for f, _ in rows]
        when = ""
        if s != "models" and span:
            mins = (max(span) - min(span)) / 60
            when = f", spanning {mins / 60:.1f}h" if mins > 90 else f", spanning {mins:.0f}m"
        print(f"  {s}  —  {len(rows)} files, {total / 1024**3:.2f} GB{when}")
        for f, t in rows[:3]:
            print(f"      {f.name}  ->  {t.relative_to(dest)}")
        if len(rows) > 3:
            print(f"      … and {len(rows) - 3} more")
    if ignored:
        print(f"\n  leaving {len(ignored)} unrecognised file(s) untouched")

    if not args.apply:
        print("\nRe-run with --apply once the sessions match what you remember printing.")
        return 0

    moved = skipped = 0
    for f, t in moves:
        if t.exists():
            print(f"  SKIP (exists): {t}")
            skipped += 1
            continue
        t.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(f), str(t))
        moved += 1
    # Tidy now-empty directories, deepest first. Never removes the roots.
    for d in sorted((p for p in src.rglob("*") if p.is_dir()),
                    key=lambda p: len(p.parts), reverse=True):
        try:
            if d != dest and not any(d.iterdir()):
                d.rmdir()
        except OSError:
            pass
    print(f"\nmoved {moved}, skipped {skipped}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

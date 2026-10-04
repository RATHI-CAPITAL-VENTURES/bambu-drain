"""Mounting the backing image and finding what the printer left on it."""

from __future__ import annotations

import fnmatch
import hashlib
import os
import shutil
import subprocess
import time
from contextlib import contextmanager
from pathlib import Path

CHUNK = 1024 * 1024


class MountError(RuntimeError):
    pass


class UnmountError(MountError):
    """The Pi still holds the image. Never swallow this one."""


# A full check of a 32 GB image takes seconds. Minutes means it is stuck, and
# the printer has no medium for as long as it is.
FSCK_TIMEOUT_SECONDS = 120


def _run(*args: str) -> str:
    proc = subprocess.run(args, capture_output=True, text=True)
    if proc.returncode != 0:
        raise MountError(f"{' '.join(args)}: {proc.stderr.strip()}")
    return proc.stdout


def is_mounted(mount_point: Path) -> bool:
    return subprocess.run(
        ["mountpoint", "-q", str(mount_point)]
    ).returncode == 0


@contextmanager
def mounted(image: Path, mount_point: Path, fs: str):
    """Mount the backing image read-write, guaranteeing the unmount.

    The caller must have ejected the medium from the gadget first. Mounting an
    image the host still has open is the one way to corrupt this setup, so the
    ordering is enforced by drain.py rather than trusted here.
    """
    mount_point.mkdir(parents=True, exist_ok=True)
    if is_mounted(mount_point):
        raise MountError(f"{mount_point} is already mounted — refusing to stack")

    fstype = "exfat" if fs == "exfat" else "vfat"
    _run("mount", "-o", "loop,noatime", "-t", fstype, str(image), str(mount_point))
    try:
        yield mount_point
    finally:
        # sync before unmount: the image is about to be handed back to a host
        # that will read it immediately.
        subprocess.run(["sync"], check=False)
        for attempt in range(5):
            if subprocess.run(["umount", str(mount_point)]).returncode == 0:
                break
            time.sleep(1.0)
        else:
            raise UnmountError(
                f"could not unmount {mount_point} — NOT re-inserting media, "
                "because the printer and the Pi would both hold it"
            )


# How each filesystem gives back clusters that are marked used and belong to no
# file: the command, and the folder it salvages them into (deleted afterwards).
# `fsck.exfat -y` alone reports such a volume "clean" and frees nothing; only
# `-s` touches orphans, by turning them into files. `fsck.vfat -a` frees them
# outright.
_RECLAIM = {
    "exfat": (("fsck.exfat", "-s", "-y"), "LOST+FOUND"),
    "fat32": (("fsck.vfat", "-a"), None),
}


def reclaim_tool(fs: str) -> str:
    return _RECLAIM[fs][0][0]


def orphaned_bytes(mount_point: Path) -> int:
    """Space the filesystem counts as used that no file or directory holds.

    Zero for anything that is not a mounted filesystem of its own: measured
    against a plain directory this would be the rest of the disk.
    """
    if not os.path.ismount(mount_point):
        return 0
    vfs = os.statvfs(mount_point)
    used = (vfs.f_blocks - vfs.f_bfree) * vfs.f_frsize
    held = 0
    for path in mount_point.rglob("*"):
        try:
            held += path.lstat().st_blocks * 512
        except OSError:
            continue
    return max(0, used - held)


# exFAT's VolumeFlags: a 16-bit field at byte 106 of the main boot sector, bit
# 1 = VolumeDirty. Excluded from the boot checksum by the spec, which is what
# lets a driver flip it on every mount.
_EXFAT_FLAGS_OFFSET = 106
_EXFAT_DIRTY = 0x2


def is_dirty(image: Path, fs: str) -> bool:
    """Is the volume flagged as not cleanly unmounted?

    exFAT only. FAT32 keeps its flag in the second FAT entry, behind a parse
    of the BPB; this deployment is exFAT and the printer's FAT32 behaviour is
    unmeasured, so FAT32 reports False rather than guessing.
    """
    if fs != "exfat":
        return False
    with image.open("rb") as fh:
        fh.seek(_EXFAT_FLAGS_OFFSET)
        flags = int.from_bytes(fh.read(2), "little")
    return bool(flags & _EXFAT_DIRTY)


def clear_dirty(image: Path, fs: str) -> bool:
    """Check and mark clean a volume left dirty. True if it was dirty.

    The printer will not mount a dirty volume — it reports the drive as "not
    formatted" — and Linux never clears a flag it found set: it only clears
    one it set itself. So one dirty mark, from a brownout mid-pass or the
    printer losing the medium mid-write, would last forever.

    Done with `fsck.exfat -p` rather than by flipping the bit: a volume that
    is flagged dirty may really need a repair, and clearing the flag without
    looking would hand the printer a broken filesystem marked healthy. The
    image must not be attached or mounted.
    """
    if not is_dirty(image, fs):
        return False
    try:
        proc = subprocess.run(["fsck.exfat", "-p", str(image)], capture_output=True,
                              text=True, timeout=FSCK_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired as exc:
        raise MountError(f"fsck.exfat still running after {exc.timeout:.0f}s") from exc
    if proc.returncode not in (0, 1) or is_dirty(image, fs):
        raise MountError(f"fsck.exfat -p: exit {proc.returncode}, volume still dirty: "
                         f"{(proc.stderr or proc.stdout).strip()[:200]}")
    return True


def reclaim(image: Path, mount_point: Path, fs: str) -> None:
    """Free orphaned clusters. The image must be ejected and unmounted."""
    cmd, salvaged = _RECLAIM[fs]
    try:
        proc = subprocess.run([*cmd, str(image)], capture_output=True, text=True,
                              timeout=FSCK_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired as exc:
        raise MountError(f"{cmd[0]} still running after {exc.timeout:.0f}s") from exc
    # fsck exits 1 for "errors found and corrected", which is the point.
    if proc.returncode not in (0, 1):
        raise MountError(f"{' '.join(cmd)}: exit {proc.returncode}: "
                         f"{(proc.stderr or proc.stdout).strip()[:200]}")
    if salvaged:
        with mounted(image, mount_point, fs) as mp:
            shutil.rmtree(mp / salvaged, ignore_errors=True)


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        while chunk := fh.read(CHUNK):
            h.update(chunk)
    return h.hexdigest()


def _patterns(glob: str) -> tuple[str, ...]:
    """A glob, plus the same glob rooted at the top level.

    `**/*.mp4` reads as "anywhere", but fnmatch requires the slash to be
    present, so it does NOT match a bare `a.mp4` sitting in the root of the
    stick — which is exactly where the printer puts things. Stripping the
    leading `**/` gives us the root-level form as well.
    """
    if glob.startswith("**/"):
        return (glob, glob[3:])
    return (glob,)


def match_rule(rel: str, rules) -> object | None:
    """First matching rule wins."""
    for rule in rules:
        if any(fnmatch.fnmatch(rel, pat) for pat in _patterns(rule.glob)):
            return rule
    return None


def candidates(mount_point: Path, rules, min_age_seconds: float):
    """Files old enough to be safe to move, with the rule that claims them.

    The age check is a second line of defence behind the idle gate: a file the
    printer finished writing seconds ago may still have buffered data on the
    host side that we would rather see land first.
    """
    now = time.time()
    for path in sorted(mount_point.rglob("*")):
        if not path.is_file():
            continue
        # FAT/exFAT recycle and system noise
        rel = str(path.relative_to(mount_point))
        if rel.startswith((".", "System Volume Information")):
            continue
        rule = match_rule(rel, rules)
        if rule is None:
            continue
        try:
            st = path.stat()
        except OSError:
            continue
        if now - st.st_mtime < min_age_seconds:
            continue
        yield path, rule, st

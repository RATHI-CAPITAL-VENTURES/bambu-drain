"""Mounting the backing image and finding what the printer left on it."""

from __future__ import annotations

import fnmatch
import hashlib
import subprocess
import time
from contextlib import contextmanager
from pathlib import Path

CHUNK = 1024 * 1024


class MountError(RuntimeError):
    pass


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
            raise MountError(
                f"could not unmount {mount_point} — NOT re-inserting media, "
                "because the printer and the Pi would both hold it"
            )


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

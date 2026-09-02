"""One drain pass at a time. Not a nicety — a correctness requirement.

`bambu-drain drain --once` is a completely reasonable thing to type while the
systemd service is running, and until this existed it raced the daemon: both
processes ejected the medium, both mounted the same loop image, and both
enumerated the same files. The mildest outcome is the one observed in testing,
a FileNotFoundError as one deleted a file the other was about to.

The severe outcome is that nothing stopped process B calling `gadget insert`
while process A still had the image mounted read-write — handing the printer a
filesystem the Pi is actively writing to. That is the exact block-level
double-mount the whole project is built to avoid, reached from the inside.

flock is the right primitive here: it is released automatically if the process
dies, so a crashed pass cannot wedge the daemon out of its own lock.
"""

from __future__ import annotations

import errno
import fcntl
import os
from contextlib import contextmanager
from pathlib import Path


class AlreadyRunning(RuntimeError):
    pass


@contextmanager
def single_instance(path: Path):
    """Hold an exclusive, non-blocking lock, or raise AlreadyRunning.

    Non-blocking on purpose: a second drain pass should say so and exit, not
    queue up behind the first and then run against a stick whose state it
    enumerated minutes ago.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            if exc.errno in (errno.EACCES, errno.EAGAIN):
                try:
                    holder = os.pread(fd, 32, 0).decode().strip() or "unknown"
                except OSError:
                    holder = "unknown"
                raise AlreadyRunning(
                    f"another bambu-drain pass is running (pid {holder})"
                ) from exc
            raise

        os.ftruncate(fd, 0)
        os.write(fd, f"{os.getpid()}\n".encode())
        try:
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)

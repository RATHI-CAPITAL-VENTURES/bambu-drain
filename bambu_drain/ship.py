"""Loop B — move what the Pi has to the Mac, at leisure.

Runs with the gadget fully attached and the printer none the wiser. If the Mac
is asleep or the Wi-Fi is out, this loop simply falls behind; the drain loop
keeps the stick empty until staging hits its budget, and only then does anything
user-visible happen.
"""

from __future__ import annotations

import logging
import shlex
import subprocess
import time
import time
from pathlib import Path, PurePosixPath

from . import render as render_mod
from .ledger import Ledger
from .lock import AlreadyRunning, single_instance

log = logging.getLogger("bambu_drain.ship")


class ShipError(RuntimeError):
    pass


def remote_abs(dest: str, rel: str, home: str) -> str:
    """The absolute remote path, tilde already expanded, NOT shell-quoted.

    Two different quoting rules apply to the same path and getting them
    backwards is silent: rsync's destination must stay raw, while `ssh mkdir`
    and `ssh shasum` need `shlex.quote`.

    Why rsync must stay raw: the Mac ships **openrsync at protocol 29**, which
    predates `--protect-args` and rejects both it and `--old-args` outright.
    Quoting the destination puts the quote characters into the filename —
    observed as `/Users/ishan/'Library/Mobile Documents/...'`. Determined
    empirically against the real pair, not from the manual.
    """
    if dest == "~":
        dest = home
    elif dest.startswith("~/"):
        dest = f"{home.rstrip('/')}/{dest[2:]}"
    base = dest.rstrip("/")
    return f"{base}/{rel}" if rel else base


def _home_arg(path: str) -> str:
    """Quote for a remote shell while leaving a leading ~ expandable."""
    if path == "~":
        return "~"
    if path.startswith("~/"):
        return "~/" + shlex.quote(path[2:])
    return shlex.quote(path)


def shell_arg(path: str) -> str:
    """Quote an already-absolute path for a remote shell command."""
    return shlex.quote(path)


class Shipper:
    def __init__(self, cfg, ledger: Ledger):
        self.cfg = cfg
        self.ledger = ledger
        self._home: str | None = None

    def remote_home(self) -> str:
        """Resolve `$HOME` on the Mac once, so no tilde ever reaches rsync."""
        if self._home is None:
            proc = self._ssh('printf %s "$HOME"')
            if proc.returncode != 0 or not proc.stdout.strip():
                raise ShipError(f"could not resolve $HOME on {self.cfg.ship.host}")
            self._home = proc.stdout.strip()
        return self._home

    def abs_path(self, rel: str) -> str:
        return remote_abs(self.cfg.ship.dest, rel, self.remote_home())

    def _ssh(self, *command: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [
                "ssh",
                "-i", str(self.cfg.ship.ssh_key),
                "-o", "BatchMode=yes",
                "-o", "ConnectTimeout=10",
                self.cfg.ship.host,
                *command,
            ],
            capture_output=True,
            text=True,
        )

    def reachable(self) -> bool:
        """Can we reach the ship host, healing a stale mDNS cache once if not.

        The Mac's DHCP lease moves, and the Pi's avahi cache holds the old
        address well past it — which surfaces as "no route to host" and reads
        like the Mac being switched off. Seen three times in two days. Using the
        `.local` name is still right; the cache just needs a nudge.
        """
        if self._ssh("true").returncode == 0:
            return True
        subprocess.run(["systemctl", "restart", "avahi-daemon"],
                       capture_output=True, check=False)
        time.sleep(2)
        return self._ssh("true").returncode == 0

    def run_once(self) -> dict:
        try:
            with single_instance(self.cfg.ship_lock_path):
                return self._run_once_locked()
        except AlreadyRunning as exc:
            log.info("%s", exc)
            return {"shipped": 0, "bytes": 0, "pending": 0, "skipped": str(exc)}

    def render_missing(self) -> int:
        """Build timelapses for finished prints that never got one.

        Runs BEFORE shipping, because shipping deletes the staged segments and
        they are the raw material. Doing it here rather than on the Mac avoids
        pulling gigabytes back out of iCloud.
        """
        if not self.cfg.render.enabled or not render_mod.available():
            return 0
        built = 0
        for session in self.ledger.sessions_needing_render():
            segments = [p for p in self.ledger.staged_segments(session) if p.exists()]
            if len(segments) < self.cfg.render.min_segments:
                continue
            folder = self.ledger.session_dest_dir(session)
            if not folder:
                continue
            out = self.cfg.drain.staging / folder / render_mod.OUT_NAME
            try:
                render_mod.render(segments, out, self.cfg.render.length_seconds,
                                  self.cfg.render.fps, self.cfg.render.crf)
            except render_mod.RenderError as exc:
                log.error("render failed for %s: %s", session, exc)
                self.ledger.event("render_error", f"{session}: {exc}")
                continue
            import hashlib
            sha = hashlib.sha256(out.read_bytes()).hexdigest()
            self.ledger.record_drained(
                sha, render_mod.OUT_NAME, f"{folder}/{render_mod.OUT_NAME}",
                out.stat().st_size, out, session=session, src_mtime=time.time())
            self.ledger.event("rendered", f"{session}: {out.stat().st_size} bytes")
            built += 1
        return built

    def _run_once_locked(self) -> dict:
        self.render_missing()
        pending = self.ledger.unshipped()
        if not pending:
            return {"shipped": 0, "bytes": 0, "pending": 0}

        if not self.reachable():
            log.info("%s unreachable; %d files waiting", self.cfg.ship.host, len(pending))
            return {"shipped": 0, "bytes": 0, "pending": len(pending), "offline": True}

        shipped = 0
        total = 0
        for row in pending:
            staging_path = row["staging_path"]
            if not staging_path:
                continue
            local = Path(staging_path)
            if not local.exists():
                log.error("staging file vanished: %s", local)
                self.ledger.event("staging_missing", row["src_name"])
                continue

            # Check OUR copy before blaming the network. When a power cut left a
            # 0-byte staged file, the ship loop rsynced the emptiness, compared
            # hashes, and reported "remote checksum mismatch" — pointing at the
            # Mac when the damage was local and the original was already gone
            # off the stick. A wrong diagnosis costs more than the failure.
            actual = local.stat().st_size
            if actual != row["size"]:
                log.error(
                    "LOCAL COPY CORRUPT: %s is %d bytes, ledger says %d. "
                    "The original is already off the stick, so this file is lost. "
                    "Not retrying.",
                    rel_name := row["src_name"], actual, row["size"],
                )
                self.ledger.event(
                    "local_corrupt",
                    f"{rel_name}: {actual}/{row['size']} bytes — unrecoverable",
                )
                self.ledger.record_shipped(row["sha256"], verified=False)
                continue

            rel = row["dest_rel"]
            target = self.abs_path(rel)
            parent_rel = str(Path(rel).parent)
            parent = self.abs_path("" if parent_rel == "." else parent_rel)

            if self._ssh(f"mkdir -p {shell_arg(parent)}").returncode != 0:
                log.error("could not create remote dir for %s", rel)
                continue

            rsync = subprocess.run(
                [
                    "rsync", "-a", "--partial", "--inplace",
                    "-e", f"ssh -i {self.cfg.ship.ssh_key} -o BatchMode=yes",
                    str(local),
                    # Raw, unquoted: see remote_abs().
                    f"{self.cfg.ship.host}:{target}",
                ],
                capture_output=True,
                text=True,
            )
            if rsync.returncode != 0:
                log.error("rsync failed for %s: %s", rel, rsync.stderr.strip())
                continue

            # Verify on the far side. iCloud will upload it afterwards on its
            # own schedule; what we assert here is that it is durably on the
            # Mac's disk, which is the thing we can actually check.
            probe = self._ssh(f"shasum -a 256 {shell_arg(target)}")
            remote_sha = probe.stdout.split()[0] if probe.returncode == 0 and probe.stdout else ""
            verified = remote_sha == row["sha256"]
            if not verified:
                log.error("remote checksum mismatch for %s — keeping staging copy", rel)
                self.ledger.event("ship_mismatch", rel)
                continue

            self.ledger.record_shipped(row["sha256"], verified=True)
            local.unlink(missing_ok=True)
            self.ledger.clear_staging(row["sha256"])
            shipped += 1
            total += row["size"]
            log.info("shipped %s (%.1f MB)", rel, row["size"] / 1024**2)

        if shipped:
            self.ledger.event("ship", f"{shipped} files, {total} bytes")
        return {"shipped": shipped, "bytes": total, "pending": len(pending) - shipped}

    def push_status(self, local: Path) -> bool:
        """Copy the status file to the Mac so RIA can read it locally.

        Best-effort and deliberately quiet: if the Mac is away this fails every
        cycle and that is not news. The staleness of the file IS the signal —
        RIA alerts on a status that has stopped being updated, which covers the
        Pi being dead, the network being down, and this push failing, without
        needing any of them to report themselves.
        """
        if not local.exists():
            return False
        remote = self.cfg.health.remote_status_path
        parent = str(PurePosixPath(remote).parent)
        if self._ssh(f"mkdir -p {_home_arg(parent)}").returncode != 0:
            return False
        proc = subprocess.run(
            [
                "rsync", "-a",
                "-e", f"ssh -i {self.cfg.ship.ssh_key} -o BatchMode=yes -o ConnectTimeout=10",
                str(local),
                f"{self.cfg.ship.host}:{remote}",
            ],
            capture_output=True, text=True,
        )
        return proc.returncode == 0

    def prune_remote(self) -> int:
        """Apply retention_days on the Mac. 0 means keep forever."""
        days = self.cfg.ship.retention_days
        if days <= 0:
            return 0
        base = self.abs_path("")
        cmd = f"find {shell_arg(base)} -type f -mtime +{days} -delete -print"
        proc = self._ssh(cmd)
        if proc.returncode != 0:
            return 0
        removed = len([ln for ln in proc.stdout.splitlines() if ln.strip()])
        if removed:
            self.ledger.event("prune", f"{removed} files older than {days}d")
        return removed

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
from pathlib import Path

from .ledger import Ledger

log = logging.getLogger("bambu_drain.ship")


class ShipError(RuntimeError):
    pass


def remote_path(dest: str, rel: str) -> str:
    """Quote a remote path that may contain spaces and a leading ~.

    `~/Library/Mobile Documents/com~apple~CloudDocs/...` is the normal case and
    it contains both hazards at once: the tilde must stay unquoted so the remote
    shell expands it, and the spaces must be quoted so it does not word-split.
    """
    full = f"{dest.rstrip('/')}/{rel}"
    if full.startswith("~/"):
        return "~/" + shlex.quote(full[2:])
    return shlex.quote(full)


class Shipper:
    def __init__(self, cfg, ledger: Ledger):
        self.cfg = cfg
        self.ledger = ledger

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
        return self._ssh("true").returncode == 0

    def run_once(self) -> dict:
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

            rel = row["dest_rel"]
            target = remote_path(self.cfg.ship.dest, rel)
            parent = remote_path(self.cfg.ship.dest, str(Path(rel).parent))

            if self._ssh(f"mkdir -p {parent}").returncode != 0:
                log.error("could not create remote dir for %s", rel)
                continue

            rsync = subprocess.run(
                [
                    "rsync", "-a", "--partial", "--inplace",
                    "-e", f"ssh -i {self.cfg.ship.ssh_key} -o BatchMode=yes",
                    str(local),
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
            probe = self._ssh(f"shasum -a 256 {target}")
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

    def prune_remote(self) -> int:
        """Apply retention_days on the Mac. 0 means keep forever."""
        days = self.cfg.ship.retention_days
        if days <= 0:
            return 0
        base = remote_path(self.cfg.ship.dest, "")
        cmd = f"find {base} -type f -mtime +{days} -delete -print"
        proc = self._ssh(cmd)
        if proc.returncode != 0:
            return 0
        removed = len([ln for ln in proc.stdout.splitlines() if ln.strip()])
        if removed:
            self.ledger.event("prune", f"{removed} files older than {days}d")
        return removed

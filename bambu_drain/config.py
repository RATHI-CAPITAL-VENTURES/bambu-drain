"""Configuration loading. stdlib only — the Pi gets no pip install."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_PATH = Path("/etc/bambu-drain/config.toml")


@dataclass(frozen=True)
class Rule:
    """One kind of file the printer leaves behind.

    `group="print"` files are filed under `prints/<session>/<dest>/`, where the
    session groups everything from one print run. Everything else keeps the
    dated `<dest>/YYYY/MM/` layout — a sliced model or a firmware image does not
    belong to a print.

    `rename` replaces the filename outright. Used for the single assembled
    timelapse, which is more useful as `timelapse.mp4` inside a print folder
    that is already named for its date than as `video_<ts>.mp4`.
    """

    glob: str
    dest: str
    delete: bool = True
    group: str = ""
    rename: str = ""
    # The printer writes this file exactly once, when a print ends — so it
    # CLOSES the session, and the next file starts a new one regardless of gap.
    #
    # This is not a refinement, it is the only thing that works. A failed print
    # and its redo were separated by 26 minutes, while gaps WITHIN a print run
    # to 18. Splitting on time alone would need a ~22 minute threshold, four
    # minutes above normal, which would fragment any print with a slow layer.
    ends_session: bool = False
    # A fraction of the modal (full) segment size, below which a file is taken
    # to have been closed EARLY — which for a rotating recording means the print
    # stopped. 0 disables it.
    #
    # This is the strongest boundary signal available, because it is physical
    # rather than inferential: the chamber recording rotates at a fixed size, so
    # every full segment is within 0.1% of every other, and anything short marks
    # a recording that was cut off. Measured across 61 segments: full ones were
    # all 240.2-240.4 MB, and every genuine print ending came in at 12-91%.
    # Nothing landed between 92% and 99%.
    ends_session_if_short: float = 0.0

    def __post_init__(self) -> None:
        if self.group not in ("", "print"):
            raise ValueError(f"rule.group must be empty or 'print', got {self.group!r}")


@dataclass(frozen=True)
class GadgetConfig:
    name: str = "bambu"
    udc: str = ""
    image: Path = Path("/srv/bambu-drain/stick.img")
    size_gb: int = 32
    fs: str = "exfat"
    vendor: str = "BambuDrn"
    product: str = "Drain"
    serial: str = "BD00000001"

    def __post_init__(self) -> None:
        if self.fs not in ("exfat", "fat32"):
            raise ValueError(f"gadget.fs must be exfat or fat32, got {self.fs!r}")
        # The SCSI INQUIRY fields are fixed-width; the kernel silently truncates
        # but a surprised printer is worse than a loud config error.
        if len(self.vendor) > 8:
            raise ValueError("gadget.vendor must be <= 8 characters")
        if len(self.product) > 16:
            raise ValueError("gadget.product must be <= 16 characters")


@dataclass(frozen=True)
class DrainConfig:
    idle_minutes: float = 5.0
    min_file_age_minutes: float = 2.0
    poll_seconds: float = 30.0
    mount_point: Path = Path("/mnt/bambu-stick")
    staging: Path = Path("/srv/bambu-drain/staging")
    staging_max_gb: float = 64.0
    max_eject_seconds: float = 120.0
    # Once the printer has been quiet this long it is not mid-print, it is
    # done — so a longer ejected window is cheap and a big backlog can drain in
    # one pass instead of a dozen.
    long_idle_minutes: float = 20.0
    max_eject_seconds_long_idle: float = 900.0
    # Files this far apart belong to different prints. Measured: segments
    # within one print land 9-18 minutes apart, and consecutive prints were 805
    # minutes apart. Anything in the 30-60 range separates them cleanly.
    #
    # This IS a heuristic. Nothing in the filenames identifies the job — no
    # print id, no model name — so two prints started inside this window merge
    # into one folder. The printer does not tell the USB drive what it is
    # recording, and no amount of parsing recovers that.
    # The fallback when no session-closing file appears. Deliberately generous:
    # fragmenting one print across two folders is worse than merging two, and
    # `ends_session` handles the common case exactly. A print that produces NO
    # timelapse, followed soon after by another, will still merge — that is a
    # real limit, not an oversight.
    session_gap_minutes: float = 45.0
    rules: tuple[Rule, ...] = ()


@dataclass(frozen=True)
class ShipConfig:
    host: str = ""
    dest: str = ""
    ssh_key: Path = Path("/home/pi/.ssh/id_ed25519")
    poll_seconds: float = 300.0
    retention_days: int = 0


@dataclass(frozen=True)
class HealthConfig:
    status_file: Path = Path("/srv/bambu-drain/status.json")
    # Where to push the status file on the ship host. RIA runs on the Mac and
    # cannot reach the Pi — macOS Local Network Privacy blocks her launchd
    # server from the LAN, the same thing that broke the lamp and Radarr. So
    # the Pi pushes and RIA only ever reads a local file.
    remote_status_path: str = "~/.bambu-drain/status.json"


@dataclass(frozen=True)
class Config:
    gadget: GadgetConfig = field(default_factory=GadgetConfig)
    drain: DrainConfig = field(default_factory=DrainConfig)
    ship: ShipConfig = field(default_factory=ShipConfig)
    health: HealthConfig = field(default_factory=HealthConfig)

    @property
    def ledger_path(self) -> Path:
        return self.gadget.image.parent / "ledger.db"

    @property
    def drain_lock_path(self) -> Path:
        return self.gadget.image.parent / "drain.lock"

    @property
    def ship_lock_path(self) -> Path:
        return self.gadget.image.parent / "ship.lock"


def _paths(d: dict, *keys: str) -> dict:
    out = dict(d)
    for k in keys:
        if k in out:
            out[k] = Path(out[k])
    return out


def load(path: Path | str = DEFAULT_PATH) -> Config:
    path = Path(path)
    with path.open("rb") as fh:
        raw = tomllib.load(fh)
    return from_dict(raw)


def from_dict(raw: dict) -> Config:
    """Split out from load() so tests can build a Config without a file."""
    rules = tuple(Rule(**r) for r in raw.get("rule", []))
    if not rules:
        raise ValueError("no [[rule]] entries — bambu-drain would drain nothing")

    drain_raw = _paths(raw.get("drain", {}), "mount_point", "staging")
    drain_raw.pop("rules", None)

    return Config(
        gadget=GadgetConfig(**_paths(raw.get("gadget", {}), "image")),
        drain=DrainConfig(rules=rules, **drain_raw),
        ship=ShipConfig(**_paths(raw.get("ship", {}), "ssh_key")),
        health=HealthConfig(**_paths(raw.get("health", {}), "status_file")),
    )

"""Configuration loading. stdlib only — the Pi gets no pip install."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_PATH = Path("/etc/bambu-drain/config.toml")


@dataclass(frozen=True)
class Rule:
    """One kind of file the printer leaves behind."""

    glob: str
    dest: str
    delete: bool = True


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


@dataclass(frozen=True)
class Config:
    gadget: GadgetConfig = field(default_factory=GadgetConfig)
    drain: DrainConfig = field(default_factory=DrainConfig)
    ship: ShipConfig = field(default_factory=ShipConfig)
    health: HealthConfig = field(default_factory=HealthConfig)

    @property
    def ledger_path(self) -> Path:
        return self.gadget.image.parent / "ledger.db"


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

# Changelog

`MAJOR.MINOR.PATCH` in `VERSION`. The `changelog` guard enforces that the top
header equals `VERSION`, is new relative to the base branch, and increases
monotonically. A MINOR bump is a milestone and must ship a retro.

## 0.2.0 — 2026-08-31

The first working implementation: the Pi presents itself to the P2S as a USB
stick and drains it continuously. See `docs/retros/0.2.0.md`.

### Added

- USB mass-storage gadget control over configfs (`gadget.py`). Changes the
  *medium* rather than unbinding the UDC, so the printer sees a card eject
  rather than a device disappearing.
- Drain loop (`drain.py`): idle-gated, checksum-verified, always re-inserts the
  medium via `finally`. Bounded by `max_eject_seconds`.
- Ship loop (`ship.py`): rsync to the Mac over SSH with remote checksum
  verification, and optional `retention_days` pruning.
- SQLite ledger (`ledger.py`) keyed on SHA-256; every delete is gated on it, so
  a crashed pass is safe to re-run.
- Registry-driven drain rules in `config.toml` — one `[[rule]]` per kind of file
  the printer leaves behind. Firmware images archive but never delete.
- `bambu-drain doctor`, which checks the setup in the order it actually breaks.
- Pi 4 setup scripts and three systemd units.
- 31 tests covering config validation, rule matching, ledger idempotency,
  drain gating, and remote path quoting.

## 0.1.0 — 2026-08-31

### Added

- Opened.

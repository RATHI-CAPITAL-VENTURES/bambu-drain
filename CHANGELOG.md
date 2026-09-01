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
- 33 tests covering config validation, rule matching, ledger idempotency,
  drain gating, and remote path handling.
- `setup/bootstrap.sh` — one privileged entry point (`prepare` / `verify` /
  `start`), so the install is a single sudo prompt rather than eight.

### Fixed

- **Concurrency: nothing serialised the drain passes.** Running
  `drain --once` while the service was live raced the daemon — both ejecting the
  medium and mounting the same image. Worst case, one re-inserted the medium
  while the other had it mounted read-write, handing the printer a filesystem
  the Pi was writing to. Both loops now hold an exclusive `flock` for the whole
  pass, taken before the gadget is touched.
- **Data loss: a verified copy was not a durable copy.** `shutil.copy2` leaves
  data in the page cache, and the read-back checksum read it straight back out
  of that cache — confirming bytes that were never written to disk. The original
  was then deleted from the stick. A power cut in that window destroyed a 150 MB
  file for real, in testing, on day one. Now `fsync_file_and_parent()` runs
  before the verify and before the delete, and the ledger uses
  `PRAGMA synchronous=FULL`.
- Ship errors blamed the wrong end: a 0-byte local file was reported as
  "remote checksum mismatch". The local copy is now checked against the ledger
  before the network is touched, and an unrecoverable file stops being retried.

- `01-enable-dwc2.sh` now parses `config.txt` **sections**. The stock image
  ships `dtoverlay=dwc2,dr_mode=host` under `[cm5]`, which is inert on a Pi 4
  but satisfies a naive grep — leaving `/sys/class/udc` empty with no error.
- Ship paths: the rsync destination must be raw and the shell arguments quoted,
  because macOS ships openrsync (protocol 29) with no `--protect-args`. Quoting
  the destination put literal quote characters in the filename.
- `.gitignore` now covers `__pycache__`, which was being committed.

Older series are archived under `docs/changelog/`.

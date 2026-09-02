# Changelog

`MAJOR.MINOR.PATCH` in `VERSION`. The `changelog` guard enforces that the top
header equals `VERSION`, is new relative to the base branch, and increases
monotonically. A MINOR bump is a milestone and must ship a retro.

## 0.3.0 — 2026-09-01

Health that reaches you. The Pi now publishes a verdict and pushes it to the
Mac, so RIA can alert without ever touching the LAN.

### Added

- **A health verdict** (`health.problems/verdict`). One definition of "healthy",
  computed on the Pi, so anything reading the status file agrees without
  reimplementing it. Deliberately **stable** during normal operation so it can
  drive a change-detecting watch: idle seconds, byte counts and file counts move
  constantly and must not move the verdict.
- **`bambu-drain status --verdict`** — one line, `ok` or `PROBLEM: …`, exit 1
  when unhealthy.
- **Status push to the Mac** (`ship.push_status`). RIA cannot reach the Pi —
  macOS Local Network Privacy blocks her launchd server from the LAN — so the Pi
  pushes and RIA only ever reads a local file.
- The verdict covers the failures that are silent by nature: an ejected medium,
  a charge-only cable, an unbound gadget, staging filling because the Mac is
  away, and data-integrity events.

### Fixed

- **The daemons never wrote the status file.** It was produced only when a human
  ran `status`, so its timestamp recorded the last time someone looked rather
  than the last time the loop ran — useless as a health signal. The drain loop
  now writes it every pass, which makes the file's age the loop's heartbeat.
- **`sudo bambu-drain <anything>` did not work.** The wrapper relied on
  `/etc/default/bambu-drain`, which only systemd reads, so the command every doc
  in this repo tells you to run failed with `No module named bambu_drain`.
  Documented repeatedly, never once executed.
- **Integrity events alarmed forever.** `problems()` scanned recent events with
  no time bound, so a `ship_mismatch` from a power cut hours earlier was still
  the reported verdict long after the cause was fixed. Bounded to one hour — an
  alert that never clears is an alert you learn to ignore.

## 0.2.1 — 2026-09-01

Documentation. Everything needed to rebuild this from scratch now lives in the
repo rather than in the heads of the people who built it. An audit found that
**none** of the following was written down anywhere: the printer's timelapse
setting, the SSH setup, passwordless sudo, the GPIO power pins, enabling Remote
Login on the Mac, checking `get_throttled`, the charge-only cable trap, or
flashing the Pi.

### Added

- **`docs/SETUP.md`** — the complete build in order: bill of materials, power
  wiring with the pin identification and the polarity warning, the data-cable
  requirement, flashing, SSH in both directions, install, connecting to the
  printer, **configuring the printer** (without which the whole system silently
  drains an empty drive), and end-to-end verification.
- **`docs/TROUBLESHOOTING.md`** — organised by symptom, because several
  unrelated causes present identically as "the printer doesn't see a USB drive".
  `not attached` vs `configured` separates them in one command.
- **`setup/00-ssh-from-mac.sh`** — automates the bidirectional SSH setup,
  including putting the Host alias in `/etc/ssh/ssh_config.d/` where root can
  see it, and verifying the return path *as root* rather than as the login user.
- A "what runs where" table making explicit that **nothing runs on the Mac** —
  it is only an SSH and rsync destination.

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

- **Thumbnails were never drained.** The rules listed `*.png`; the P2S writes
  `.jpg` into `/ipcam/thumbnail/` and `/timelapse/thumbnail/`. They would have
  accumulated forever. Found by running a real print.
- **The medium could be left out, and the printer says nothing useful.** If a
  pass died between eject and insert, the printer was left looking at a card
  reader with no card — it reports "no USB drive", which is indistinguishable
  from the cable falling out and goes unnoticed until a print fails. Every drain
  pass now re-inserts an absent medium before any other gate, including while a
  print is running. `doctor` checks it, and also reports the USB session state
  with the charge-only-cable hint.
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

# Changelog

`MAJOR.MINOR.PATCH` in `VERSION`. The `changelog` guard enforces that the top
header equals `VERSION`, is new relative to the base branch, and increases
monotonically. A MINOR bump is a milestone and must ship a retro.

## 0.3.2 — 2026-09-02

### Fixed

- **Our own deletions reset the idle clock.** Removing files from the stick
  updates the backing image's mtime — the exact signal used to detect the
  printer writing — so every drain pass reset its own gate. Invisible on a small
  drain; on a 4-hour print's 5.4 GB backlog it meant four passes over ~25
  minutes with the medium ejected eight times. `quiet_seconds()` now looks
  through the mtime our own pass caused.
- **The eject budget now scales with confidence.** Five minutes of quiet is the
  minimum bar for believing a print ended; twenty is near certainty. Below
  `long_idle_minutes` the window stays at 120 s, above it 900 s — enough for a
  multi-gigabyte backlog in one pass.

### Changed

- Corrected the idle-gate documentation. The "writes every 1–8 seconds" figure
  came from one short print and does not generalise: timelapse capture is per
  layer, so the interval depends on the model. The claim that actually holds is
  stronger — **the gate held for an entire 4-hour print**, zero drain or eject
  events from first layer to last.
- Documented that the ipcam recording is **segmented**: ~240 MB every 11–14
  minutes, 22 segments and 5.4 GB for a 4-hour print. Size staging for that, not
  for the 190 MB a short print produces.

## 0.3.1 — 2026-09-01

### Added

- **MIT licence.** Without one, "open source" is not legally open — default
  copyright means nobody may use it.
- A scope note in the README: this was built and run against a P2S, a Pi 4 and
  macOS, and only that combination has actually been exercised.

### Changed

- Commit history rewritten to remove `claude.ai` session URLs from the four
  commit messages that carried them. Content is byte-identical (verified by tree
  hash); only messages changed. Done before publishing, because after the first
  fork it cannot be.

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

Older series are archived under `docs/changelog/`.

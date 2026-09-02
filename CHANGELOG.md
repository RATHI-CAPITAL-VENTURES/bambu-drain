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

Older series are archived under `docs/changelog/`.

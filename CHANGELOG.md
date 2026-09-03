# Changelog

`MAJOR.MINOR.PATCH` in `VERSION`. The `changelog` guard enforces that the top
header equals `VERSION`, is new relative to the base branch, and increases
monotonically. A MINOR bump is a milestone and must ship a retro.

## 0.7.1 — 2026-09-03

### Fixed

- **ffmpeg was never a declared dependency.** Rendering shipped in 0.7.0 without
  it appearing in any setup script, in `SETUP.md`, or in `doctor`. On a fresh
  install `render.available()` would return False and the ship loop would skip
  rendering **without a word** — an enabled feature that is silently off, which
  is the failure this project keeps rediscovering. `setup/03-install.sh` now
  installs it and `doctor` checks for it whenever `[render] enabled = true`.

## 0.7.0 — 2026-09-03


Missing timelapses are rebuilt automatically, on the Pi.

### Added

- **`[render]` — the ship loop rebuilds a timelapse before shipping.** The P2S
  usually keeps the assembled timelapse on its own internal storage, so a print
  arrives as hours of chamber footage and nothing to watch. This fills the gap
  while the segments are still on the Pi.

  It runs **before** shipping on purpose: shipping deletes the staged segments,
  and they are the raw material. Doing it on the Mac instead would mean pulling
  gigabytes back out of iCloud — slow, and the thing that has bitten this
  project twice.

- **Keyframes only**, which is what makes it viable on a Pi 4. Measured on one
  real 750 s segment:

  | approach | time |
  |---|---|
  | full decode + select every 275th frame | 294 s |
  | `-skip_frame nokey`, every 9th keyframe | **66 s** |

  A 4.4-hour print is 21 segments: 103 minutes against 23. The footage carries
  ~774 keyframes per segment — about one per second, far more than the ~1800
  frames a 60-second timelapse needs — and an I-frame is a full-quality picture
  rather than a reconstructed one.

- A session is only rendered when it is **closed**, its segments are **still
  staged**, and it has **no timelapse of either kind**. Rendering a running
  print would be worse than useless; rendering a shipped one is impossible; and
  the third condition stops us overwriting the printer's own.

Older series are archived under `docs/changelog/`.

# Changelog

`MAJOR.MINOR.PATCH` in `VERSION`. The `changelog` guard enforces that the top
header equals `VERSION`, is new relative to the base branch, and increases
monotonically. A MINOR bump is a milestone and must ship a retro.

## 0.4.1 — 2026-09-02

### Added

- **`tools/make_timelapse.py`** — reconstructs a timelapse from the chamber
  segments when the printer kept the assembled one on internal storage. Samples
  the real 1080p30 footage (~495,000 frames for a 4.6-hour print) rather than the
  per-segment thumbnails, of which there are 22 — under a second of video.
  Output is `timelapse-reconstructed.mp4`, never `timelapse.mp4`.
- It refuses to start when segments are still evicted to iCloud, because
  `ffprobe` blocks indefinitely on a placeholder rather than failing.

A PATCH bump on purpose: the drain and ship loops are untouched. This is a
manual utility beside them, not a change to what the system does on its own.

### Changed

- Documented that the assembled timelapse **may not reach the USB drive at all**.
  Observed on three prints: two short ones wrote it to the drive, a 4.6-hour one
  did not. `prints/<session>/timelapse.mp4` being absent therefore says nothing
  about whether the drain worked.

## 0.4.0 — 2026-09-02


One folder per print, and a configurable archive name.

### Added

- **`prints/<date_time>/` grouping.** Everything from one run — video segments,
  thumbnails and the timelapse — lands together instead of scattering across
  dated type folders. A 4-hour print puts 22 segments in one place.
- **`ends_session` on a rule.** The printer writes its timelapse exactly once,
  when a print ends, so that file closes the session.
- **`tools/regroup_archive.py`** — reorganises an existing archive with the same
  logic. Dry run by default; moves, never deletes; never overwrites.
- `rule.group`, `rule.rename` and `session_gap_minutes`.

### Changed

- **The archive folder name is yours to choose.** It was always the last
  component of `ship.dest`, but the example hardcoded `BambuArchive` as though it
  were fixed. The example now uses an obvious placeholder and says so.

### Fixed

- **A failed print and its redo were filed as one print.** The first version
  grouped by time gap alone, and the real boundary was 26 minutes against
  within-print gaps of 18 — so no threshold separates them without risking
  fragmenting a print. The result put a 0.1 MB timelapse from a cancelled job at
  the root of a 4-hour print that had none of its own. The timelapse now ends the
  session.

Older series are archived under `docs/changelog/`.

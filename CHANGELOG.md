# Changelog

`MAJOR.MINOR.PATCH` in `VERSION`. The `changelog` guard enforces that the top
header equals `VERSION`, is new relative to the base branch, and increases
monotonically. A MINOR bump is a milestone and must ship a retro.

## 0.4.2 — 2026-09-02

### Fixed

- **A 0-byte export claimed the canonical `timelapse.mp4`.** The printer
  exported an empty timelapse beside a real 15.9 MB one; sorted first, the empty
  file took the name and the real one was pushed to `timelapse-b79482df.mp4`.
  An empty file now keeps its own filename, which makes it obviously not the
  timelapse.
- **`doctor` now checks that some rule closes print sessions.** A config
  predating a feature loads fine and silently loses it — which is exactly what
  happened: the code supported `ends_session`, the deployed config did not, and
  every timelapse recorded `ends=0` until someone read the ledger.

### Known limit, now documented

- **Manually exported timelapses all share the export time.** Copying them from
  the printer's internal storage to USB stamps every file with the moment of the
  copy, so they group by *when you exported* rather than by which print they
  belong to. The true timestamp survives only in the filename
  (`video_2026-09-02_21-51-18.mp4`), and the printer's clock is ~12 h off, so it
  is usable for ordering but not for absolute time.

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

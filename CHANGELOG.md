# Changelog

`MAJOR.MINOR.PATCH` in `VERSION`. The `changelog` guard enforces that the top
header equals `VERSION`, is new relative to the base branch, and increases
monotonically. A MINOR bump is a milestone and must ship a retro.

## 0.6.0 — 2026-09-03


Prints are named after their model, and everything about a print lives in one
folder.

### Added

- **`starts_session` — the sliced file opens a print.** The printer writes the
  `.gcode.3mf` when the job is *sent*, ~15 minutes before the first chamber
  segment. That makes it the only START marker available; every other signal can
  only say a print has ended.
- **`names_session` — and it names the folder**, so a print reads as
  `2026-09-02_2017_Steamer_Cable_Holder_v1`.
- The sliced file now lives **inside its print folder** rather than a separate
  `models/` tree. It belongs to that print.
- **`TEARDOWN_SECONDS`** — a print's last segment and its timelapse are flushed
  together and both end the session. Without this, whichever sorted first closed
  the print and the other opened a spurious one-file session.

### Fixed

- **`regroup_archive.py` duplicated the model-name and modal-size logic**, and
  the duplicate kept an exact-value mode long after the real one moved to a
  median — so boundary detection in the migration was silently disabled while
  the daemon's worked. It now imports the shared implementation.
- **The migration reclassified its own output as printer data.** A 169 MB
  `timelapse-reconstructed.mp4` read as a chamber segment at 70% of the rotation
  size, which looks like a print ending, and split one print into two.

### Known limit

Bambu Studio names the sent file after the **process preset** whenever the
Studio project is unnamed, so roughly half arrive as
`0.2mm layer, 2 walls, 15% infill`. Those are detected and the folder keeps a
plain timestamp. Slicing strips the mesh objects, so there is no better name
inside the file — this is the only signal there is.

Older series are archived under `docs/changelog/`.

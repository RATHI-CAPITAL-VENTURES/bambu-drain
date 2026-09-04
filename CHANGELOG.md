# Changelog

`MAJOR.MINOR.PATCH` in `VERSION`. The `changelog` guard enforces that the top
header equals `VERSION`, is new relative to the base branch, and increases
monotonically. A MINOR bump is a milestone and must ship a retro.

## 0.8.0 — 2026-09-03


### Added

- **Real-time bookends on the timelapse.** At one frame per ~9 seconds of print,
  the first minute — levelling, purge, first layer — lasted about **0.2 seconds**
  and the end was equally invisible. `head_seconds` and `tail_seconds` (5 s each
  by default) now play the opening and closing at normal speed, either side of
  the sped-up body.

### Fixed

- **The frame estimate counted the last segment as full.** A print's final
  segment is always short — that is how the ending is detected — so the body
  came out shorter than requested. Negligible across twenty segments, 50% wrong
  across two.

Older series are archived under `docs/changelog/`.

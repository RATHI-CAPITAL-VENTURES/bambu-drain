# Changelog

`MAJOR.MINOR.PATCH` in `VERSION`. The `changelog` guard enforces that the top
header equals `VERSION`, is new relative to the base branch, and increases
monotonically. A MINOR bump is a milestone and must ship a retro.

## 0.9.0 — 2026-09-04


### Added

- **The head clip now opens on the printer working, not parked.** The recording
  starts before the print does — levelling, heating, the head sitting still — so
  a 5-second opening was 5 seconds of nothing. `motion_start()` finds the first
  **sustained** motion and seeks there.

  Measured on a real first segment: seconds 1–5 scored **0.000**, sustained
  motion began at **29 s**, the purge showed around 41–49 s. Detection returns
  29 s on that footage.

  "Sustained" is load-bearing — the same segment had a lone `0.037` blip at
  `t=0` with silence either side, which an instantaneous threshold would have
  believed.

- `skip_dead_air` and `dead_air_cap` (120 s). A failed detection returns 0 and
  degrades to the previous behaviour; the cap means a print that genuinely
  begins slowly loses at most two minutes rather than its whole opening.

Only the head needs this. At one frame per ~9 seconds the body already renders
half a minute of idling as three frames.

Older series are archived under `docs/changelog/`.

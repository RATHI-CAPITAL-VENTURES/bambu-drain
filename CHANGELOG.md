# Changelog

`MAJOR.MINOR.PATCH` in `VERSION`. The `changelog` guard enforces that the top
header equals `VERSION`, is new relative to the base branch, and increases
monotonically. A MINOR bump is a milestone and must ship a retro.

## 0.5.0 — 2026-09-03


Print boundaries from a physical signal instead of a guess.

### Added

- **`ends_session_if_short` — a short chamber segment ends a print.** The
  recording rotates at a fixed size, so every full segment is within 0.1% of
  every other and a short one was closed early, meaning the print stopped. This
  is the first boundary signal that is physical rather than inferential, and the
  only one that works when the timelapse went to internal storage.

  Measured over 61 real segments: full ones were all 240.2–240.4 MB, every
  genuine ending came in at 12–91%, and **nothing landed between 92% and 99%**.

- The rotation size is **learned from history**, not configured — it is a
  property of the printer and firmware.

- The ship loop now **restarts `avahi-daemon` once** when the host is
  unreachable. The Mac's DHCP lease moves and the Pi's mDNS cache holds the old
  address past it, which surfaces as "no route to host" and reads like the Mac
  being off. Seen three times in two days.

### Fixed

- **`modal_size` returned `None` for 61 perfectly good segments**, silently
  disabling boundary detection. It grouped by exact byte count, and real
  segments differ by a few kilobytes — so no size ever repeated. Uses the
  median now, which is also robust to the minority of short segments.

Older series are archived under `docs/changelog/`.

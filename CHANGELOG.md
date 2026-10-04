# Changelog

`MAJOR.MINOR.PATCH` in `VERSION`. The `changelog` guard enforces that the top
header equals `VERSION`, is new relative to the base branch, and increases
monotonically. A MINOR bump is a milestone and must ship a retro.

## 0.10.1 — 2026-10-04

### Docs

- **The 10-03 reboot was the supply being swapped, not a brownout.** 0.10.0's
  ARCHITECTURE, TROUBLESHOOTING and retro said the Pi rebooted unprompted.
  Corrected, with the per-hour under-voltage counts: none for 11.5 hours on
  the new supply, then ~70 an hour, tracking drain passes. The dirty flag
  predates that reboot.

## 0.10.0 — 2026-10-04


### Fixed

- **The printer called the drive "not formatted" and stopped using it.** The
  exFAT VolumeDirty flag was set, and the printer will not mount a dirty
  volume. Linux never clears a flag it found already set, so one mark lasted
  forever; it only started to matter when 0.9.7 made the printer remount after
  every drain. The drain now checks the flag after each pass and at `gadget
  create`, and runs `fsck.exfat -p` when it is set. `status` reports a dirty
  stick as a problem.
- **The 0.9.7 deploy crashed the drain service once:** drain and ship start
  together and both added the new ledger columns; the second hit "duplicate
  column". That is now tolerated.

Retro: `docs/retros/0.10.0.md`.

Older series are archived under `docs/changelog/`.

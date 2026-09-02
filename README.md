# bambu-drain

A Raspberry Pi that pretends to be a USB stick, so the Bambu Lab P2S never
needs its storage cleaned out by hand.

The printer writes timelapses and sliced files to what it believes is an
ordinary flash drive. It is a backing image on a Pi 4. When the printer goes
quiet, the Pi ejects the media, empties it onto the Mac's iCloud Drive folder,
and puts it back. The printer notices nothing except that the stick is
permanently almost empty.

## Why not just read the files over the network?

Bambu printers do expose the whole filesystem over FTPS — but only with
**LAN-Only Mode + Developer Mode**, which are mutually exclusive with Bambu
cloud. That costs you Handy, remote monitoring, MakerWorld one-click send, and
cloud print history.

Pretending to be a USB stick costs nothing. The printer's networking is
untouched, so the cloud stays exactly as it was. That is the entire reason this
project has the shape it does.

## How it works

Two loops that never block each other:

**Loop A — drain (`bambu-drain drain`).** Watches the backing image's mtime.
When the printer hasn't written for `idle_minutes`, it ejects the medium, mounts
the image locally, copies matching files to the Pi's staging directory, verifies
each by SHA-256, deletes the originals, unmounts, and re-inserts. Seconds, not
minutes — it is a local disk-to-disk copy.

**Loop B — ship (`bambu-drain ship`).** Runs with the stick fully attached and
the printer none the wiser. rsyncs staging to the Mac over SSH, verifies the
remote checksum, then drops the Pi's copy. If the Mac is asleep this loop simply
falls behind; nothing user-visible happens until staging hits its budget.

The split exists because copying a 300 MB timelapse over Wi-Fi would hold the
printer's storage hostage for minutes. Loop A's ejected window is bounded by
local disk speed and by `max_eject_seconds`.

## The one thing that could corrupt data

USB mass storage is **block-level**. The printer owns the filesystem. If the Pi
mounts the image while the printer still has it, two kernels are caching and
writing one filesystem with no coordination, and you lose the lot.

Everything in `drain.py` exists to make that impossible: the medium is ejected
before the mount, the mount is wrapped in a `finally` that always re-inserts,
and the eject uses the kernel's `forced_eject` so a printer holding the medium
open is an error rather than a silent overlap.

## Setup

**→ [docs/SETUP.md](docs/SETUP.md)** is the complete build, in order, from a bare
Pi to a verified working system. It covers the parts that are easy to skip and
expensive to get wrong:

- the bill of materials, and why **a Pi 5 will not work** (its USB-C is
  power-only and cannot do peripheral mode)
- **power** — the Pi 4's USB-C becomes the data port, so 5 V goes to GPIO pins
  4 and 6; how to identify them without killing the board, and the supplies that
  are known not to work
- **the cable** — most USB-C ↔ USB-A cables are charge-only, which looks
  identical to a broken setup
- **SSH in both directions**, including why the alias must be system-wide
- **configuring the printer** — timelapse must be set to save to *External*, and
  without it the whole system silently drains an empty drive

`setup/00-ssh-from-mac.sh` automates the SSH half.

**→ [docs/TROUBLESHOOTING.md](docs/TROUBLESHOOTING.md)** is organised by symptom,
because several unrelated causes present identically as "the printer doesn't see
a USB drive".

**→ [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)** is the reasoning: why this
approach over FTPS, the block-level hazard, and the traps found on real hardware.

### The short version

```sh
# on the Mac
setup/00-ssh-from-mac.sh rpi ishan-mac
rsync -a --exclude .git ./ rpi:~/bambu-drain/

# on the Pi
sudo setup/bootstrap.sh prepare && sudo reboot
ls /sys/class/udc                       # MUST be non-empty
sudo nano /etc/bambu-drain/config.toml  # ship.host, ship.dest, ssh_key
sudo setup/bootstrap.sh verify          # gadget + doctor + dry-run
sudo setup/bootstrap.sh start
```

Then **set the printer's timelapse save location to External/USB**, or nothing
will ever reach the drive.

## What runs where

| Machine | What |
|---|---|
| **Pi** | three systemd services: the gadget, the drain loop, the ship loop |
| **Mac** | **nothing.** It is only an SSH + rsync destination — no daemon, no launchd agent |
| **Printer** | nothing installed; one setting changed |

## Before you trust it

```sh
sudo bambu-drain drain --once --dry-run
```

Reports what it would move and deletes nothing. Run a print, look at the output,
and only then start the service. The first week is worth watching.

## exFAT or FAT32?

`exfat` has no 4 GB per-file limit and is the default here. **FAT32 will
truncate a timelapse that grows past 4 GB.** If you are unsure whether the P2S
mounts exFAT, format FAT32, run one long print, and check the video plays to the
end — then decide. It is one command to reformat.

## Day to day

```sh
bambu-drain status        # gadget, drain gate, staging, archive totals
bambu-drain status --json # same, machine-readable, also written to status.json
bambu-drain doctor        # checks the setup in the order it breaks
```

The failure worth watching for is a silent one: the drain loop stops, staging
fills, and the first symptom is the printer refusing to print three weeks later.
`status` surfaces `staging_pct` for exactly that reason.

## Configuration

See `config.example.toml`. What to drain is a registry, not a branch — one
`[[rule]]` per kind of file the printer leaves behind, with `dest` deciding the
archive folder and `delete` deciding whether the original goes. Firmware images
are archived but never deleted, because the printer needs them to stay put to
apply an update.

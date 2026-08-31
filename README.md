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

## Hardware

A **Pi 4 Model B** or a **Pi Zero 2 W**. Not a Pi 5 — its USB-C is power-only
and cannot do peripheral mode at all.

On a Pi 4 the USB-C port is both the power input and the only port that can act
as a USB device. Once `setup/01-enable-dwc2.sh` has run, that port is a data
line to the printer, so:

- power the Pi from a **5 V / 3 A supply on GPIO pin 2 or 4 (5V) and pin 6 (GND)**
- use a **VBUS-blocking adapter** on the USB-C line so the printer isn't
  back-feeding 5 V into an already-powered Pi
- use a **USB-C → USB-A** cable into the port on top of the printer

A Pi 4 browning out mid-write is how the backing image gets corrupted.

## Install

On the Pi — one privileged entry point, so it is a single sudo prompt:

```sh
sudo setup/bootstrap.sh prepare       # boot config + image + install
sudo reboot
ls /sys/class/udc                     # MUST be non-empty; nothing works otherwise

sudo nano /etc/bambu-drain/config.toml   # set ship.host, ship.dest, ssh_key
sudo setup/bootstrap.sh verify        # gadget + doctor + dry-run drain
sudo setup/bootstrap.sh start         # enable and start the three services
```

The individual `01-`/`02-`/`03-` scripts still exist and are safe to re-run on
their own; `bootstrap.sh` just sequences them.

### The destination alias must resolve for **root**

The drain loop needs configfs and `mount`, so the services run as root. A
`Host` block in `~/.ssh/config` belonging to your login user is invisible to
root, and `doctor` reports the Mac as unreachable — which reads like a network
fault and is not one. Put it system-wide instead:

```sh
sudo tee /etc/ssh/ssh_config.d/10-bambu-drain.conf <<'EOF'
Host ishan-mac
  HostName your-mac.local
  User you
  IdentityFile /home/rpi/.ssh/id_ed25519
  IdentitiesOnly yes
  StrictHostKeyChecking accept-new
EOF
```

Use the Mac's **mDNS `.local` name, not its IP** — the IP is a DHCP lease and
will move. It moved once during this project's own setup.

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

# Troubleshooting

Symptoms first, because several completely unrelated causes present identically.
Everything here was hit for real during the first build.

## Start here

```sh
sudo bambu-drain doctor
```

Nine checks, ordered so the first failure is the root cause rather than a
consequence.

---

## "The printer doesn't see a USB drive"

Three unrelated causes look exactly like this. One command separates them:

```sh
cat /sys/class/udc/fe980000.usb/state
```

### `not attached`

No host, no VBUS session. In order of likelihood:

1. **The cable is charge-only.** By far the most common. Most USB-C ↔ USB-A
   cables carry power only. Test it by plugging a phone into a computer with it.
2. **You blocked VBUS.** If you taped pin 1 of the USB-A plug, undo it — on a Pi
   4 with `dr_mode=peripheral`, `dwc2` needs VBUS to detect a host.
3. The printer's port is inactive, or the cable is not seated.

### `configured` but the printer still shows nothing

The link is fine — the printer enumerated the Pi. The problem is the medium:

```sh
cat /sys/kernel/config/usb_gadget/bambu/functions/mass_storage.usb0/lun.0/file
```

Empty means the gadget is a **card reader with no card**, which the printer
reports as "no USB drive". A pass died between eject and insert. Every drain
pass now re-inserts an absent medium automatically, but you can force it:

```sh
sudo bambu-drain gadget insert
```

---

## `/sys/class/udc` is empty

`dwc2` is not in peripheral mode, so the Pi cannot be a USB device at all.

**`/boot/firmware/config.txt` is sectioned**, and this is the trap: the stock
Raspberry Pi OS image ships `dtoverlay=dwc2,dr_mode=host` under a **`[cm5]`**
section. That line is inert on a Pi 4 but satisfies a naive grep for the
overlay, so a check can "find" it and skip. Only lines at the top of the file or
under `[all]` apply.

```sh
grep -nE '^\[|dtoverlay=dwc2' /boot/firmware/config.txt
```

You need, under `[all]`:

```
dtoverlay=dwc2,dr_mode=peripheral
```

plus `modules-load=dwc2` in `cmdline.txt` (one line, no newlines) and
`libcomposite` in `/etc/modules`. `setup/01-enable-dwc2.sh` handles all three and
is section-aware. **A reboot is required.**

If you are on a **Pi 5**, stop — its USB-C is power-only and cannot do
peripheral mode. Use a Pi 4 or a Zero 2 W.

---

## Nothing is ever drained; `archived` stays at 0

Most likely **the printer is writing to internal storage**, not the USB drive.
Set the timelapse save location to **External / USB** — see
[SETUP Part 6](SETUP.md#part-6--configure-the-printer). Some firmware defaults
to Internal, and everything else will look perfectly healthy.

Otherwise check the gate:

```sh
sudo bambu-drain status
```

- `printer active (Ns since last write)` — working as designed. The gate opens
  after 5 minutes of quiet.
- `staging over budget` — the ship loop is behind; see below.

---

## Files drain but never reach the Mac

```sh
ssh rpi 'sudo ssh -o BatchMode=yes ishan-mac hostname'
```

Note the `sudo`. The services run as **root**, and a `Host` alias in the login
user's `~/.ssh/config` is invisible to root — `doctor` then reports the Mac as
unreachable, which reads like a network problem. The alias belongs in
`/etc/ssh/ssh_config.d/`. See [SETUP Part 3](SETUP.md#pi--mac).

Other causes:

- **Remote Login is off on the Mac.** System Settings → General → Sharing.
  Checking with `lsof -iTCP:22` as a normal user gives a false negative; it
  cannot see root's `sshd`.
- **The Mac's IP changed.** Use its mDNS `.local` name, never a DHCP address.
- **The Mac is asleep.** The ship loop falls behind harmlessly until staging
  hits its budget, then the drain loop stops too, on purpose.

### `rsync` errors mentioning a path with literal quotes in it

Something like `/Users/you/'Library/Mobile Documents/...'`. macOS ships
**openrsync at protocol 29**, which predates `--protect-args` and rejects both
it and `--old-args`. The rsync destination must be passed **raw and unquoted**;
only shell commands (`ssh mkdir`, `shasum`, `find`) get `shlex.quote`. This is
handled in `ship.py` — if you see it, something reintroduced quoting there.

---

## `LOCAL COPY CORRUPT ... this file is lost`

The staged copy on the Pi does not match the size the ledger recorded, and the
original has already been deleted from the stick. That file is unrecoverable.

This is what an unclean shutdown used to cause: a copy verified out of the page
cache, deleted from the stick, then lost to a power cut. `fsync` before the
verify and delete fixed the cause. If you see this message, check for brownouts:

```sh
vcgencmd get_throttled
```

---

## A print produced chamber video but no timelapse

Check the archive: if `prints/<session>/` has a full `video/` folder and no
`timelapse.mp4`, the printer never wrote one. Nothing was lost in transit —
confirm with `sudo bambu-drain status`, and check the stick is empty.

Two causes, and the difference matters:

1. **The per-job timelapse setting was off.** It is per-job in Bambu Studio, so
   re-slicing and re-sending a failed print can silently drop it.
2. **The save location reverted to Internal.** Check Handy: Device > Timelapse >
   Internal Storage. If the missing timelapse is there, the External setting is
   not sticky — and this project will keep missing them until it is fixed. See
   [SETUP Part 6](SETUP.md#part-6--configure-the-printer).

## The Mac was reachable and now is not: "no route to host"

The Pi's mDNS cache went stale after the Mac's DHCP lease moved. It looks like
the Mac is down; it is not.

```sh
ssh rpi 'getent hosts <your-mac>.local'   # compare against the Mac's real IP
ssh rpi 'sudo systemctl restart avahi-daemon'
```

Using the `.local` name rather than an IP is still correct — the address moved
three times during this project's first two days. The cache just needs a nudge.

## Under-voltage

```sh
vcgencmd get_throttled
```

| Value | Meaning |
|---|---|
| `0x0` | healthy |
| bit 0 (`0x...1`) | under-voltage **right now** — fix the supply |
| bit 2 (`0x...4`) | CPU throttled right now |
| bit 16 (`0x50000`) | under-voltage has occurred since boot — usually just the boot inrush spike |
| bit 18 | throttling has occurred |

Known-bad supplies, both tested: an **old 5 W (1 A) phone charger**
(under-volts at idle) and an **Arduino/Elegoo Mega's 5V pin** (its linear
regulator cannot source what a Pi 4 needs under any input). The printer's own
USB port cannot do it either — it lights the LED and browns out.

If a 2 A+ charger still under-volts, the loss is in the cable: thin conductors
and hand-twisted joints easily drop the ~0.4 V that trips the flag. Measure
**across pins 4 and 6 while running** — you want ≥ 4.9 V. Measuring at the
charger tells you nothing, because the cable drop is the whole problem.

---

## `another bambu-drain pass is running`

Working as designed. The daemon holds an exclusive lock and you ran a manual
pass. Wait, or stop the service first. The lock exists because concurrent passes
could mount the image while the other re-inserted the medium to the printer.

---

## Timelapse filenames are dated wrong

The P2S's own clock. Ours was ~12 hours off. Archive paths use the file's mtime
so the folder structure is correct; only the printer's filenames lie.

---

## Timelapse video truncated at 4 GB

That would mean the drive is FAT32, which caps single files at 4 GB — and the
printer truncates before this code ever sees the file, so nothing here can
recover it. The P2S **does** mount exFAT (verified), which has no such limit, so
this should not happen. Check:

```sh
sudo blkid /srv/bambu-drain/stick.img    # expect TYPE="exfat"
```

To reformat, move the image aside and re-run `setup/02-create-image.sh 32 exfat`.

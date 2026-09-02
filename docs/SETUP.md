# Setup — from a bare Pi to a printer that never fills up

Every step, in order, including the ones that are easy to skip and expensive to
get wrong. If you are rebuilding this from scratch, follow it top to bottom.

```
   Bambu P2S ──USB-A──┐
                      │  (data cable — see Part 1)
              Raspberry Pi 4 ──wifi──► your Mac ──► iCloud Drive
                      ▲
                   5V/3A on GPIO (the USB-C port is busy being the data port)
```

**Nothing runs on the Mac.** It is only an SSH + rsync destination. There is no
launchd agent, no daemon, no software to install there — just Remote Login
enabled and an SSH key authorised. All three services run on the Pi.

---

## Part 0 — Bill of materials

| Item | Notes |
|---|---|
| **Raspberry Pi 4 Model B** or **Pi Zero 2 W** | **NOT a Pi 5** — its USB-C is power-only and cannot do peripheral mode at all |
| microSD card, 32 GB+ | holds the OS *and* the staging area; bigger is better |
| **5 V / 3 A supply with bare leads**, or a USB-A → DuPont pigtail | see Part 1 |
| **USB-C → USB-A cable that carries DATA** | see Part 1; this is not the default |
| Multimeter | optional but strongly recommended for Part 1 |

A **Pi Zero 2 W** avoids the whole power problem — it has *separate* power and
data ports, so you plug a supply into one and the printer into the other. On a
Pi 4 the single USB-C port must do one job or the other, which is what makes
Part 1 fiddly. Everything here runs unchanged on either board.

---

## Part 1 — Power and cabling (do this thinking first)

### Why the Pi can't just be powered by the printer

It cannot. A Pi 4 idles around **600–700 mA** and spikes past **1.2 A**. A
printer's USB-A port is built for flash drives — typically 500 mA. It will light
the Pi's red LED, which looks like success, and then brown out during boot. The
red LED only means "5 V is present", not "enough current is available".

Also ruled out, both tested:

- **An old 5 W (1 A) phone charger** — under-volts at idle, before any load.
- **An Arduino/Elegoo Mega's 5V pin** — its linear regulator cannot source what
  a Pi 4 needs under *any* input. At 12 V in it thermally shuts down; at 5 V in
  the regulator has no headroom and produces nothing usable. There is no pin
  combination that works.

### Wiring the power

On a Pi 4 the USB-C port becomes the *data* port (Part 4), so power goes to the
GPIO header:

```
5 V  ──► pin 4
GND  ──► pin 6
```

Finding them: the **even-numbered pins** are the column nearest the **outer edge
of the board**, and **pin 1/2 is the end furthest from the USB and Ethernet
ports**. Counting down that outer column from that end:

```
1st = pin 2   5V
2nd = pin 4   5V    ◄── red
3rd = pin 6   GND   ◄── black
```

Pins 4 and 6 are adjacent, which makes this easy once you have the right column.

> **Check polarity twice.** GPIO power bypasses the Pi's polyfuse and protection
> circuitry entirely. Reversed leads kill the board instantly with no warning.
> **Pin 1 is the 3.3 V rail** — one column over. Putting 5 V into it destroys the
> Pi. This is the only step in the whole build that can cost you hardware.

**Verify before connecting anything**, with the Pi on its normal USB-C supply.
All GND pins are electrically common with the metal shells of the USB and
Ethernet ports, which gives you a reference needing no pin counting:

1. Continuity mode, black probe on a **USB port's metal shell**. Header pins that
   **beep** are GND.
2. DC volts, same reference. Pins reading **~5.0 V** are your 5V pins; pins
   reading **3.3 V** are the rail to avoid.

Then unplug everything before wiring the supply in.

### The data cable

**Most USB-C ↔ USB-A cables are charge-only.** This produces a failure that
looks exactly like a broken setup: the printer reports no USB drive and nothing
in any log explains it.

Confirm your cable carries data before blaming anything else — plug a phone into
your computer with it and see if the computer enumerates the phone.

`/sys/class/udc/fe980000.usb/state` is the diagnostic once you are wired up:

| state | Means |
|---|---|
| `not attached` | no host, no VBUS — **usually a charge-only cable** |
| `powered` / `addressed` | link up, enumeration incomplete |
| `configured` | the printer enumerated the Pi ✅ |

**Do not block VBUS.** Earlier guidance in the wild suggests taping pin 1 of the
USB-A plug to stop the printer back-feeding 5 V. On a Pi 4 with
`dr_mode=peripheral` that is unnecessary and appears to be harmful — `dwc2` uses
VBUS to detect that a host is attached, and a plain unmodified cable works.

---

## Part 2 — Flash the Pi and first boot

Use Raspberry Pi Imager. **Raspberry Pi OS Lite (64-bit)** is enough — nothing
here needs a desktop. In the Imager's advanced settings:

- set a hostname (this guide assumes `rpi`)
- set a username (this guide assumes `rpi` — Pi OS no longer defaults to `pi`)
- **enable SSH**
- configure your Wi-Fi

Boot it and confirm you can reach it: `ssh rpi@<address>`.

---

## Part 3 — SSH, both directions

Two separate links are needed and it is easy to set up only the first:

| Direction | Why |
|---|---|
| **Mac → Pi** | so you can administer it |
| **Pi → Mac** | the ship loop rsyncs archives to the Mac |

### On the Mac: enable Remote Login

System Settings → General → Sharing → **Remote Login**, or:

```sh
sudo systemsetup -setremotelogin on
```

> Checking this with `lsof -iTCP:22` as a normal user gives a false negative —
> it cannot see root's `sshd`. Use `sudo lsof` or just read the Settings pane.

### Mac → Pi

```sh
# ~/.ssh/config on the Mac
Host rpi
  HostName 192.168.x.x
  User rpi
  IdentityFile ~/.ssh/id_ed25519
  IdentitiesOnly yes
```

```sh
ssh-copy-id -i ~/.ssh/id_ed25519.pub rpi
```

### Pi → Mac

```sh
ssh rpi 'ssh-keygen -t ed25519 -N "" -C "bambu-drain@rpi" -f ~/.ssh/id_ed25519'
ssh rpi 'cat ~/.ssh/id_ed25519.pub' >> ~/.ssh/authorized_keys
```

Now the alias the Pi will use. **It must be system-wide, not in the user's
`~/.ssh/config`** — the services run as root (they need configfs and `mount`),
and root cannot see another user's SSH config. The symptom is `doctor` reporting
the Mac as unreachable, which reads like a network fault and is not one:

```sh
ssh rpi 'sudo tee /etc/ssh/ssh_config.d/10-bambu-drain.conf' <<'EOF'
Host ishan-mac
  HostName your-mac.local
  User you
  IdentityFile /home/rpi/.ssh/id_ed25519
  IdentitiesOnly yes
  StrictHostKeyChecking accept-new
EOF
```

> **Use the Mac's mDNS `.local` name, not its IP.** The IP is a DHCP lease and
> will move — it moved during this project's own setup.

Verify both:

```sh
ssh rpi 'true'                                  # Mac → Pi
ssh rpi 'sudo ssh -o BatchMode=yes ishan-mac hostname'   # Pi → Mac, AS ROOT
```

### Passwordless sudo on the Pi (optional but convenient)

Every operation needs root. Without this you will type a password constantly:

```sh
ssh -t rpi 'echo "rpi ALL=(ALL) NOPASSWD: ALL" | sudo tee /etc/sudoers.d/010_rpi-nopasswd >/dev/null \
  && sudo chmod 440 /etc/sudoers.d/010_rpi-nopasswd && sudo visudo -c'
```

`visudo -c` must print `parsed OK`. Reverse it with
`sudo rm /etc/sudoers.d/010_rpi-nopasswd`.

---

## Part 4 — Install

Copy the repo to the Pi and run the bootstrap. One privileged entry point, so
it is a single sudo prompt rather than eight:

```sh
rsync -a --exclude .git ./ rpi:~/bambu-drain/
ssh rpi 'cd ~/bambu-drain && sudo setup/bootstrap.sh prepare'
ssh rpi 'sudo reboot'
```

`prepare` does three things: puts the USB-C port into peripheral mode, creates a
32 GB exFAT backing image, and installs the package, config and systemd units
(without enabling them).

**After the reboot, this must be non-empty:**

```sh
ssh rpi 'ls /sys/class/udc'      # expect: fe980000.usb
```

If it is empty, `dwc2` did not come up in peripheral mode and nothing else will
work. See [TROUBLESHOOTING](TROUBLESHOOTING.md#sysclassudc-is-empty).

### Configure

```sh
ssh rpi 'sudo nano /etc/bambu-drain/config.toml'
```

Set at minimum:

```toml
[ship]
host    = "ishan-mac"        # the alias from Part 3
dest    = "~/Library/Mobile Documents/com~apple~CloudDocs/BambuArchive"
ssh_key = "/home/rpi/.ssh/id_ed25519"   # must match the Pi's real username
```

### Verify and start

```sh
ssh rpi 'cd ~/bambu-drain && sudo setup/bootstrap.sh verify'   # gadget + doctor + dry-run
ssh rpi 'cd ~/bambu-drain && sudo setup/bootstrap.sh start'    # enable the three services
```

`doctor` should report nine green checks.

---

## Part 5 — Connect it to the printer

Plug the **data** cable from the Pi's USB-C into the USB-A port on top of the
P2S. Confirm the printer enumerated it:

```sh
ssh rpi 'cat /sys/class/udc/fe980000.usb/state'   # expect: configured
ssh rpi 'sudo bambu-drain doctor'                 # medium inserted + host attached
```

---

## Part 6 — Configure the printer

⚠️ **Without this step the whole system silently does nothing.** It installs
cleanly, all checks pass, and it drains an empty drive forever.

On the P2S, set the **timelapse save location to External / USB** rather than
Internal. Some firmware versions default to Internal, and then the recordings
never reach the drive the Pi is presenting.

Also turn timelapse **on** for prints you want captured.

Confirm by running a short print and checking that files appear:

```sh
ssh rpi 'sudo bambu-drain status'   # "archived" should climb after the print
```

---

## Part 7 — Verify it end to end

Run a short print with timelapse on, then watch:

```sh
ssh rpi 'sudo bambu-drain status'
```

What should happen, unattended:

1. **During the print** the gate reads `printer active`. The P2S writes every
   1–8 seconds; the idle gate is 5 minutes, so it cannot fire mid-print.
2. **~5 minutes after the last write** the gate opens, the medium ejects, files
   are copied and verified, the originals are deleted, the medium goes back in.
3. **Shortly after** they appear in your iCloud folder, checksum-verified.

The P2S writes **two** recordings per print:

```
/ipcam/ipcam-record.<ts>.N.mp4    chamber camera — the big one, ~190 MB
/ipcam/thumbnail/<same>.jpg
/timelapse/video_<ts>.mp4         the timelapse — under 1 MB
/timelapse/thumbnail/<same>.jpg
```

`/ipcam/index/` is left alone; the printer maintains it.

### Check the power is actually adequate

```sh
ssh rpi 'vcgencmd get_throttled'
```

| Result | Meaning |
|---|---|
| `0x0` | healthy |
| bit 16 set (`0x50000`) | under-voltage *has occurred* — usually only the boot inrush spike, tolerable |
| bit 0 set (`0x50005`) | under-voltage **right now** — fix the supply |

Marginal power is survivable but not ideal: since the durability fix a brownout
costs a reboot rather than a file, but it is still a supply worth replacing.

---

## What runs where

| Machine | What |
|---|---|
| **Pi** | `bambu-drain-gadget` (creates the USB gadget at boot), `bambu-drain` (drain loop), `bambu-drain-ship` (ship loop) |
| **Mac** | nothing. SSH destination only. |
| **Printer** | nothing installed; one setting changed (Part 6) |

Units live in `deploy/` and are installed to `/etc/systemd/system/` by
`setup/03-install.sh`.

## Day to day

```sh
sudo bambu-drain status          # gadget, drain gate, staging, archive totals
sudo bambu-drain status --json   # machine-readable; also written to status.json
sudo bambu-drain doctor          # nine checks, in the order they break
sudo journalctl -u bambu-drain -f
```

The failure worth watching for is silent: the drain loop stops, staging fills,
and the first symptom is a print failing weeks later. `status` reports
`staging_pct` for exactly that reason.

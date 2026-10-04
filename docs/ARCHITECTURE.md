# Architecture

## The problem

The Bambu Lab P2S writes timelapse video and sliced files to storage that fills
up and has to be emptied by hand. The goal is to never do that again.

## Options considered

### 1. FTPS over the LAN — rejected

Bambu printers expose the whole filesystem over implicit FTPS on port 990
(user `bblp`, password = LAN access code). It is by far the simplest thing to
build: no hardware, no kernel modules, a cron job and forty lines of `ftplib`.

**Rejected because it requires LAN-Only Mode + Developer Mode**, and Bambu
documents Developer Mode as mutually exclusive with the cloud. The cost is
Bambu Handy, remote monitoring away from home, the camera feed when out,
MakerWorld one-click send-to-printer, and cloud print history. Firmware updates
need cloud flipped back on or a sideload.

That is a large, permanent tax on the printer's usability to solve a storage
chore. Worth recording that the FTPS route also has real traps if anyone
revisits it: the connection is **implicit** TLS (not `AUTH TLS`), the cert is
self-signed, the printer advertises `0.0.0.0` in its PASV response so the client
must substitute the control connection's host, and some firmware requires TLS
session reuse on the data channel.

### 2. Pi as a USB mass-storage gadget — chosen

The Pi presents a backing image to the printer as an ordinary USB drive. The
printer's network configuration is untouched, so **the cloud stays on**. This is
the only reason this approach beats option 1 despite being considerably more
work.

Two facts make it viable, both verified before any code was written:

- The **P2S has a real USB-A host port** on the top, officially supported for
  print files and firmware updates. (This is a genuine difference from the
  P1S/X1C generation, which are microSD machines where the card cannot be in the
  printer and in a Pi at the same time. Do not carry that assumption over.)
- The P2S **can be configured to write timelapses to the USB drive** rather than
  internal storage. Confirmed on the actual machine — this is load-bearing. If
  timelapses went to internal storage only, the gadget would drain an empty
  bucket and the project would be pointless.

### 3. Emulating the SD card — not pursued

A Pi cannot act as an SD *device*; there is no peripheral mode for the SD
interface. It would need an FPGA. Out of scope, and unnecessary given the USB
port exists.

### 4. A bigger card / stick — the honest fallback

Defers the chore rather than removing it, but keeps the cloud and costs nothing
to build. Named here so the trade is explicit: if this project ever becomes more
maintenance than it saves, this is the thing to fall back to.

## The block-level hazard

USB Mass Storage is a **block** protocol, not a file protocol. The host — the
printer — owns the filesystem on the medium. The Pi exports a raw image.

If both mount that filesystem read-write at once, two independent kernels cache
and write its metadata with zero coordination. This is not a race that can be
won with careful ordering inside one process; it is data loss.

So the Pi never mounts the image while the printer holds it. `drain.py` enforces:

1. `gadget.cycle_out()` — eject the medium, then settle.
2. `imagefs.mounted()` — a context manager that syncs and unmounts on the way
   out, and raises rather than returning if the unmount fails.
3. `finally: gadget.cycle_in()` — unconditional. If anything above blew up, the
   printer still gets its stick back. A printer with a stick beats a complete
   drain pass.

Ejecting is done with the kernel's `forced_eject` where available (Linux ≥ 5.15).
Without it, a printer holding the medium open returns `EBUSY`, and we surface
that as an error rather than mounting underneath it.

**Why change the medium rather than unbind the UDC.** Writing an empty string to
`lun.0/file` is exactly a card reader with the card pulled out: the USB device
stays enumerated and only the media goes away. Unbinding the UDC makes the
whole device vanish and re-enumerate, which is a much ruder thing to do to a
machine that may be mid-job — so a pass that changed nothing still only changes
the medium. A pass that deleted something has to do the ruder thing, below.

### A media change does not make the printer re-read the stick (found 2026-10-02)

The printer reported "not enough storage left on USB" and had saved no chamber
recording for three prints. `bambu-drain status` said `ok` throughout, and the
stick held 3 MB of files.

What was on disk, measured with the image attached read-only:

- `dump.exfat`: 302,248 of 1,048,416 clusters marked used — **9.2 GB allocated
  on a filesystem holding two files.**
- Walking the allocation bitmap: 140 runs, the large ones 240.3 MB each — the
  size of a chamber segment plus its thumbnail.
- Hashing the JPEG at the head of each run against the ledger: **63 of 63 were
  files the Pi had drained and deleted**, between 2026-09-02 and 2026-09-28.

So clusters the Pi freed were being marked used again, and only the printer
writes to the image besides us. The explanation that fits: the printer does not
remount on a media change. It keeps the allocation bitmap it loaded when the
drive was first plugged in, never sees what the Pi frees, counts its own free
space down from that first mount, and writes its stale bitmap blocks back over
ours whenever it allocates. That last part is inference — nothing on the Pi can
see inside the printer — but it predicts all three measurements, and it
predicts the timing: the Pi last rebooted (a real re-enumeration) on 09-23, the
printer wrote 28 GB to a 32 GB stick after that, and stopped recording on 09-30.

Directory entries were not affected (no deleted file ever reappeared), which is
why this hid for a month: every drain looked right, every file arrived, and the
printer's idea of free space quietly diverged from the disk's.

Two fixes, both in the drain pass:

1. **A pass that changed the filesystem reconnects the drive.**
   `gadget.cycle_in(reconnect=True)` unbinds the UDC, inserts the medium, and
   binds again, so the printer meets a freshly plugged-in stick and mounts it
   from scratch. It then waits up to 5 s for the controller to read
   `configured`, because a status snapshot taken mid-enumeration says "printer
   not attached". A pass that died between unbind and bind is healed at the
   start of the next one, the same way an absent medium is.
2. **Orphaned space is reclaimed.** With the stick mounted, the pass compares
   `statvfs` used space with what the files and directories actually hold;
   above `RECLAIM_ABOVE_BYTES` (256 MB, one segment) it unmounts, runs
   `fsck.exfat -s -y`, and deletes the `LOST+FOUND` that produces. Logged as a
   `reclaimed` event, or `reclaim_error`. It is skipped on a truncated pass
   (files not yet copied are still on the stick), bounded by a 120 s timeout,
   and not repeated for the same orphan figure — a missing or ineffective fsck
   would otherwise run, and disconnect the drive, on every poll.

Things tried that did not work, so nobody tries them again:

- **`fsck.exfat -y` reports the volume `clean` and frees nothing.** Orphaned
  clusters are only handled by `-s`, which turns them into files. On 1.2.9 that
  made 135 files totalling 9.3 GB; deleting them took the stick to 384 KB used.
- **`fsck.exfat -n` cannot be used to detect the problem** for the same reason.
  The bitmap count against the file sizes is the only signal.

### …and a remount exposes a dirty volume, which the printer calls "not formatted" (found 2026-10-04)

Two days after the reconnect shipped, the printer showed the drive as **not
formatted**. The Pi had the medium in, the controller read `configured`, and a
fresh enumeration (`new address 1` in `dmesg`) produced no write to the image
at all: the printer was enumerating the drive and declining to mount it. The
last time it had written was the reconnect after the Oct 2 20:00 drain.

The boot region was fine — signature, checksums, backup all matched, and
`fsck.exfat -n` said `clean`. One thing was set: **VolumeFlags = 0x2,
VolumeDirty.** Clearing it with `fsck.exfat -p` and reconnecting had the
printer writing within one second.

Why it is set and why it stays set:

- Linux sets VolumeDirty when it mounts read-write and clears it on unmount —
  but **only if it was clean when it mounted.** A flag it finds already set is
  left set. That is why every drain pass for weeks has logged `Volume was not
  properly unmounted`: one dirty mark, once made, is permanent.
- What made the first mark is not known. A brownout mid-pass would do it, and
  this Pi under-volts (below); so would the printer losing the medium mid-write.
- It never mattered before 0.9.7, because the printer never remounted. The
  reconnect fix made every drain a remount, and every remount met a dirty
  volume. The manual fix on 10-02 worked only because the `fsck -y` that
  preceded it had cleared the flag as a side effect.

Fix: after every pass's unmount, and at `gadget create` (boot), the drain reads
the two flag bytes and, if VolumeDirty is set, runs `fsck.exfat -p` — a check,
not a blind bit-flip, because a volume flagged dirty may really need repair.
`volume_cleaned` / `volume_dirty` events record it, and `status` reports a
dirty stick as a problem, since that is what the printer sees as unformatted.

The details that make it safe, each found by review before it shipped:

- **A clean counts as a change, so the drive reconnects.** The likeliest case
  is an idle printer that refused the dirty volume and so wrote nothing; with
  nothing deleted, a plain media change would hand back a clean volume the
  printer never re-reads.
- **Not on a truncated pass or a dry run**, for the reason reclaim isn't:
  undrained files are still on the stick for fsck to "repair".
- **Never under a live mount.** `clear_dirty` refuses if any loop device is
  backed by the image (a pass SIGTERMed mid-mount leaves one), and the boot
  path takes the drain lock first.
- **One retry an hour after a failure.** A printer refusing the volume stays
  idle, so the gate is always open; without the backoff it would fsck and
  log every 30 s.
- **`status` reports the flag as unknown while the Pi has it mounted**, since
  Linux holds it set for its own mount mid-pass.

Observed after the fix: the printer held the drive for four hours and left
VolumeFlags at `0x0`, so clean is the steady state, not an assumption.
FAT32 keeps its flag in the second FAT entry and is not handled: this
deployment is exFAT and the printer's FAT32 behaviour is unmeasured.

The Pi was also under-volting at the time, on a supply fitted the day before
(the 10-03 13:25 reboot was that swap, not a brownout — first written up here
as unprompted, which was wrong). Per hour from `dmesg`: none for the first 11.5
hours, then ~70 an hour from 01:00 to 05:40; one in the hour after the drain
loop was stopped; five in the first minute after it was restarted. So it
tracks drain activity, but not only drain activity, since the loop ran all of
the first 11.5 hours too. Not explained; a supply margin problem, not a code
one — see TROUBLESHOOTING.

Also seen and left alone: with the printer idle, a pass runs every
`poll_seconds`, so the medium is ejected and re-inserted about 2,800 times a
day and every mount logs `Volume was not properly unmounted`. Those passes
change nothing on disk and so do not reconnect.

## The idle signal

The drain loop must know when the printer is not writing. The obvious source is
the printer's MQTT status — which needs LAN mode, which is the thing we are
avoiding. Circular.

Instead: **the mtime of the backing image**. The gadget driver writes through to
the backing file with ordinary VFS calls, so any SCSI WRITE the printer issues
moves it forward. No network, no MQTT, no cloud, no credentials. `idle_minutes`
of no movement means the printer is done with the stick.

A second, independent gate — `min_file_age_minutes` — skips individual files
that were written very recently, in case host-side buffering means the last
block has not landed.

## Two loops, deliberately separate

| | Loop A (`drain`) | Loop B (`ship`) |
|---|---|---|
| Moves | stick image → Pi staging | Pi staging → Mac/iCloud |
| Gadget | ejected for the duration | fully attached |
| Speed | local disk, seconds | Wi-Fi, minutes |
| If it fails | stick starts filling | staging starts filling |

They are separate because a slow network must never extend the window in which
the printer has no storage. Loop A's cost is bounded by local disk speed and by
`max_eject_seconds`, after which it hands the medium back and defers the rest to
the next pass.

The consequence is that the delete from the stick happens **before** the file
reaches the Mac. That is deliberate: the file is verified by SHA-256 onto the
Pi's own disk first, and the Pi is durable storage. The chain is
copy → verify → delete-from-stick → ship → verify-remotely → delete-from-staging,
and every step is recorded in the ledger so any crash is safe to re-run.

## Durability: why a verified copy was not a safe copy

The chain in "Two loops" says copy → verify → delete-from-stick. That was
wrong, and a power cut proved it within a day of the software being installed.

`shutil.copy2` leaves the data in the **page cache**. The read-back checksum
then reads it *from that same cache* and confirms bytes that have never touched
the disk. The verify passes honestly and means nothing. We then delete the
original off the stick — destroying the only durable copy — and a power loss in
that window loses the file outright.

That is exactly what happened: a 150 MB file was drained, verified, deleted from
the stick, and left as a **0-byte staging file** by an unclean shutdown two
minutes later. ext4's delayed allocation had journalled the directory entry but
never written the data. `dmesg` showed `EXT4-fs: orphan cleanup`. The file was
unrecoverable.

The fix is `fsync_file_and_parent()` before the verify and before the delete —
the file's own descriptor *and* its parent directory, because durable data
behind a non-durable directory entry is still unreachable. The ledger runs
`PRAGMA synchronous=FULL` for the same reason: it gates every delete, so a
commit lost to a power cut orphans a staged file that nothing will ever ship.

**The general lesson, worth carrying to anything else that deletes a source
after copying it:** a checksum proves the bytes are *correct*, not that they are
*saved*. Those are different claims and only `fsync` makes the second one.

### Diagnose the right end

The same incident logged `remote checksum mismatch`. The Mac was fine — our own
staged file was empty. Blaming the far end sends you debugging a network that
was never broken while actual data loss goes unnoticed. `ship.py` now checks the
local copy's size against the ledger *before* touching the network, and reports
a truncated staged file as unrecoverable local loss rather than retrying against
the remote forever.

## A print's files are held until it ends

Loop B does not ship a session's files while that print is still running.

This is not tidiness — it is what makes rebuilding a missing timelapse possible
at all. **Shipping deletes the staged segments, and the segments are the raw
material.** Ship them mid-print and there is nothing left to render from.

That was not obvious, and it shipped broken. A 5.79 GB print takes several drain
passes; the ship loop ran between them, saw a session that had not closed yet,
shipped what was staged and cleared it. By the time the final short segment
closed the session, exactly one segment remained — below `min_segments` — so the
render was skipped silently. **Every print large enough to need more than one
pass lost its timelapse.** The ones that worked were small enough to drain in a
single pass, which made the feature look correct.

### And the wedge that fix would have created

Holding files introduces a new way to stop the system: a print that never closes
— the printer switched off mid-run, or a final segment that happens to arrive
full-size — would hold its files *and everything queued behind them* until
staging hit its budget and the drain loop stopped.

`max_hold_hours` (6) ships such a session anyway, without a rebuilt timelapse.
Losing a timelapse is a far better failure than a stopped drain, and the two
guards compose: the hold protects the render, the timeout protects the pipeline.

Files with no session — a sliced model, anything ungrouped — are never held.

### The hold has a hole: a backlog escapes it (found 2026-09-23, open)

The timeout compares the session's newest **printer mtime** against the
six-hour window, and a backlog is drained in mtime order, oldest first. The
Pi was power-cycled for a rewiring while the Mustang print was on its last
segment; when it came back it drained 59 segments recorded over the previous
nine hours. On the first ship pass, the newest segment drained so far carried
an mtime nine hours old — "timed out" — so sixteen segments shipped and were
cleared from staging before a newer one lifted the session back into the hold.
When the truncated final segment then closed it, the render ran on the 43
still staged: a timelapse that begins two and a half hours in, with the
"dead air" skip applied to the middle of the print.

The fix is to measure the hold from when the session was last **drained**, not
when its newest file was written — a session whose files are still arriving is
not stalled, whatever their timestamps say. Not yet applied.

The same power cut left segment 69 without a `moov` atom — the printer never
closed it — so it is unreadable by ffmpeg, the real-time tail bookend was
dropped, and the "short final segment" that ended the session was a cut cable,
not a finished print. Segments 1-10 of that print never reached the stick at
all: the ledger has no record of them, so they went to the printer's internal
storage while the Pi was off.

## Ledger

`ledger.db` (SQLite, WAL) keys on the file's SHA-256. `known()` gates every
delete. A pass that dies between "copied" and "deleted" re-runs and simply
deletes; a pass that dies between "uploaded" and "verified" keeps the staging
copy and retries.

## The medium must never be left out

A gadget with no backing file is a card reader with no card. The printer reports
"no USB drive" — the same thing it says when the cable has fallen out, or when
the cable is charge-only — so the failure is silent and diagnosed wrongly.

Every drain pass therefore re-inserts an absent medium **before any other
gate**, including the idle gate. Healing has to happen while a print is running,
because that is precisely when the printer needs its storage and when the idle
gate would otherwise skip the pass entirely. It is safe there because the pass
holds the drain lock, so nothing else is legitimately mid-cycle with the medium
out on purpose.

### Diagnosing "the printer doesn't see it"

`/sys/class/udc/fe980000.usb/state` separates causes that look identical:

| state | Means |
|---|---|
| `not attached` | no host, no VBUS — **usually a charge-only cable** |
| `powered` / `addressed` | link up, enumeration incomplete |
| `configured` | the host enumerated us; any remaining problem is the medium or the filesystem |

Observed in exactly that order during the first hookup: a charge-only USB-C
cable gave `not attached`; a real data cable gave `configured` but with the
medium out, so the printer saw an empty reader. Both look like "it doesn't work"
and have nothing in common. `doctor` now reports this line.

## One pass at a time

`bambu-drain drain --once` is a reasonable thing to type while the service is
running, and until `lock.py` existed it raced the daemon. Both processes ejected
the medium, mounted the same loop image, and enumerated the same files.

The observed symptom was mild — a `FileNotFoundError` as one deleted a file the
other was about to. The unobserved danger was not: nothing stopped process B
calling `gadget insert` while process A held the image mounted read-write,
handing the printer a filesystem the Pi was actively writing to. That is the
block-level double-mount this whole design exists to prevent, reached from the
inside rather than from the printer.

Both loops now hold an exclusive `flock` for the entire pass, taken *before* the
gadget is touched. It is non-blocking on purpose: a second pass should say so
and exit, not queue behind the first and then act on a stick state it enumerated
minutes ago. flock releases automatically when a process dies, so a crashed pass
cannot wedge the daemon out of its own lock.

## Staging budget

`staging_max_gb` blocks the drain loop when the Pi is filling up. Without it, a
broken ship loop would trade the printer's full disk for the Pi's — a strictly
worse failure, because the Pi is also the thing that fixes it.

## Environment traps, found on real hardware

Both were found during the first install and both are the same shape: a check
that *looks* like it passed.

### `config.txt` is sectioned, and the stock image already has a dwc2 line

Raspberry Pi OS ships `dtoverlay=dwc2,dr_mode=host` under a **`[cm5]`** section.
On a Pi 4 Model B that line is inert — but it is still a `dtoverlay=dwc2` line,
so a naive `grep` finds it, concludes the overlay is configured, and skips.
`/sys/class/udc` then stays empty forever with no error anywhere.

There are therefore two independent ways to get this wrong: the wrong
`dr_mode`, and the right `dr_mode` in a section that never applies. Only a line
at the top of the file or under `[all]` counts, and `setup/01-enable-dwc2.sh`
now parses sections rather than grepping. It appends under `[all]` and leaves
inert lines alone, reporting them.

### The Mac's rsync is openrsync, and the destination must NOT be quoted

macOS ships **openrsync, protocol 29**. It predates `--protect-args` and
rejects both that and `--old-args` outright. A `shlex.quote`-ed destination
does not get unquoted by anything, so the quote characters land in the
filename — observed as `/Users/ishan/'Library/Mobile Documents/...'`.

So one path is consumed under two different rules, and getting them backwards
fails silently:

| Consumer | Rule |
|---|---|
| `rsync` destination | raw, absolute, **never quoted** |
| `ssh mkdir` / `shasum` / `find` | `shlex.quote` |

`remote_abs()` resolves `$HOME` on the Mac once so no tilde ever reaches rsync,
and `shell_arg()` is the quoted form. This was determined empirically against
the real pair, not from the manual.

### Services run as root, so the SSH alias must be system-wide

The drain loop needs configfs and `mount`. A `Host` block in the login user's
`~/.ssh/config` is invisible to root, and `doctor` then reports the Mac as
unreachable — which reads like a network fault. The alias belongs in
`/etc/ssh/ssh_config.d/`, defined once.

Related: point it at a name that does not move. It was the Mac's mDNS
`.local` name until v0.9.2 (the IP is a DHCP lease and moved during this
project's own setup); since 2026-09-15 it is the Mac's **tailnet** name, which
also survives the Mac changing networks. `ship.py`'s `reachable()` still
restarts `avahi-daemon` once before giving up — harmless on a tailnet, and the
right nudge for anyone still on Part 3 alone.

### `ssh -n` and a heredoc: an empty file, silently

`ssh -n host 'sudo tee file' <<EOF` looks like a remote write and is not one.
`-n` is "stdin from `/dev/null`", so the heredoc never leaves the client and
`tee` truncates the target to nothing. The Tailscale setup script did exactly
this to the Pi's system-wide `ishan-mac` alias on its first real run — the
failure that would have followed is the one this whole section is about, "the
Mac is unreachable", with no network fault behind it. The rule is: `-n` only
on a command with no input, and read back anything a heredoc was meant to write.

### Tailscale SSH takes port 22, and check mode refuses BatchMode

`tailscale up --ssh` was in the script for convenience. It makes Tailscale
answer port 22 on the tailnet address instead of sshd, and the default ACL runs
it in *check mode*: every session must be re-approved in a browser. A `BatchMode`
client sees the auth URL and times out, so the script's own Mac → Pi
verification could never pass, and neither could any unattended admin over the
tailnet. The key-based sshd the loops already use is the right transport; the
tailnet only changes the address it is reached at. Rejected, and the script
clears the flag on a Pi that was brought up with it.

## What the P2S actually writes

Observed on a real print, which is the only way this was ever going to be
accurate:

```
/ipcam/ipcam-record.<ts>.N.mp4      chamber camera — SEGMENTED, see below
/ipcam/thumbnail/<same>.jpg
/ipcam/index/
/timelapse/video_<ts>.mp4           the assembled timelapse — under 1 MB
/timelapse/thumbnail/<same>.jpg
```

Two recordings per print, not one, and the **big** one is the ipcam record.

**The ipcam recording is segmented**, which only shows up on a long print. The
`N` is a segment counter: a 4-hour print produced segments `.3` through `.24`,
one roughly every **11–14 minutes**, each about **240 MB** — 5.4 GB in total,
against 190 MB for a short print. Size your staging budget for the long case. The
first version of `config.example.toml` listed `*.png` for thumbnails; the
printer writes **`.jpg`**. Those would have accumulated forever, which defeats
the entire premise. `*.jpg`/`*.jpeg` are now rules.

`/ipcam/index/` is left alone — the printer maintains it, and it is small.

### The idle gate, measured

On a short print the P2S writes to the stick every **1–8 seconds**, and the gate
read `printer active` for its whole duration, opening 319 seconds after the last
write.

**Do not generalise from that.** Timelapse capture is per layer, so the interval
depends entirely on the print — a large, slow model writes far less often than a
small fast one. The claim that survives is the stronger one anyway: on a
**4-hour print the gate held for the entire run**, with zero drain or eject
events between the first layer and the last. That is the evidence the design
rests on, not a cadence figure from one unrepresentative sample.

The signal is purely local — the backing file's mtime. No MQTT, no LAN mode, no
cloud.

### Our own writes look exactly like the printer's

The flaw that followed from that, found by a 4-hour print: **deleting files off
the stick updates the backing image's mtime**, which is the same signal used to
detect the printer writing. So every drain pass reset its own idle clock.

That is invisible on a small drain and awful on a large one. The print left
5.4 GB across 22 segments; `max_eject_seconds` truncated each pass at 120
seconds, and the next chunk then waited the full 5-minute gate again because the
clock had been reset by our own deletions. Four passes, ~25 minutes, the medium
ejected and re-inserted eight times.

Two fixes, both in `drain.py`:

- `quiet_seconds()` remembers the mtime our own pass caused and the printer's
  from before it, and looks through the former. A real printer write still moves
  the clock; ours does not.
- `eject_budget()` scales the window with confidence. Five minutes of quiet is
  the minimum bar for believing a print ended; twenty is near certainty. Below
  `long_idle_minutes` the budget stays at the conservative 120 s; above it, 900 s
  — enough for a multi-gigabyte backlog in one pass.

### The printer's clock is not yours

Files came back named `2026-09-02_07-48-39` from a print that ran at
`2026-09-01 20:00` local. The P2S clock was ~12 hours off. Archive paths use the
file's mtime, so this only affects the printer's own filenames, but do not trust
those timestamps when looking for a specific print.

## The timelapse may not reach the USB drive at all

Observed, and it changes what this project can promise:

| print | chamber video (USB) | assembled timelapse |
|---|---|---|
| short (12 min) | yes | on the USB drive |
| short (18 min, failed) | yes | on the USB drive |
| **long (4.6 h)** | **yes, 22 segments, 5.06 GB** | **internal storage only** |

The chamber recording reliably lands on the drive. The assembled timelapse does
not always — for the long print it was written to the printer's internal
storage, where nothing here can reach it. Whether that is a duration threshold,
a setting that did not persist, or something else is **not established**; we have
three prints and two behaviours.

The practical consequence is that `prints/<session>/timelapse.mp4` can be absent
for a print that completed perfectly well, and its absence says nothing about
whether the drain worked.

### Reconstructing one from the chamber footage

`tools/make_timelapse.py` builds a timelapse from the segments instead. The raw
material is far better than it sounds: 1920x1080 at 30 fps, 750 s per segment,
so a 4.6-hour print is ~495,000 frames. Sampling one frame per ~9 seconds of
print gives a 60-second clip.

The per-segment thumbnails are **not** an alternative, which is worth stating
because it is the first idea anyone has: there is one thumbnail per segment, so a
4-hour print yields 22 stills — under a second of video.

The output is named `timelapse-reconstructed.mp4`, never `timelapse.mp4`. One is
what the printer made and the other is what we assembled, and an archive that
blurs the two lies about its own provenance.

## Grouping by print

The archive is one folder per print:

```
prints/3DBenchy_09_02_26/
  3DBenchy.gcode.3mf
  timelapse.mp4            or timelapse-reconstructed.mp4
models/2026/09/     sliced models keep the dated layout — they belong to no print
```

### Only the model and the timelapse are kept

The chamber segments and thumbnails are still drained, grouped and staged
exactly as before — sessions are inferred from them, and a missing timelapse is
rebuilt from them. They are just not shipped. A rule marked
`discard_after_timelapse` records that on the ledger row, and the ship loop
deletes the staged copy once **a timelapse of that print is checksum-verified
on the Mac**. Before that the archive took `video/` (5 GB for a 4-hour print)
and `thumbnails/` as well.

The condition is "verified on the Mac", not "rendered": a rebuilt timelapse
sits in staging, not fsynced, until it ships, and deleting its segments first
would let one power cut take both. So within a ship pass the kept files go
first and raw material last, and each raw file is one of three things:

| The print has… | Raw material is… |
| --- | --- |
| a timelapse verified on the Mac | deleted from staging (`discard` event) |
| a timelapse that has not got across yet | left staged, retried next pass |
| no timelapse, or one that was lost | shipped to `video/` and `thumbnails/`, as before |

"Has not got across yet" means staged, intact and unshipped
(`Ledger.timelapse_coming`) — not merely that a row exists. A rebuilt timelapse
truncated by a power cut is marked shipped-unverified and never retried;
footage that waited on it would wait until staging filled and the drain
stopped. That was the first thing an independent review of this change found.

The last row is deliberate. Fewer than `render.min_segments`, a render that
failed, or a session that timed out unclosed all leave the footage as the only
record of the print, and it is kept rather than thrown away on a technicality.

A discarded row stays in the ledger with `discarded_at` set: it is how a
re-drained copy is recognised, and the modal segment size is computed from
those rows. `status` no longer counts them as archived.

"Has a timelapse" is decided by `Ledger.timelapses()` — a non-empty
`timelapse*.mp4` directly in the print folder. The render check used
`dest_rel LIKE '%timelapse%.mp4'`, which also matched every segment of a print
whose model was named "Timelapse stand"; harmless when it only skipped a
render, not when it decides a deletion. Both now share the one definition.

Prints already in the archive keep their `video/` and `thumbnails/` folders.
Nothing here reaches back to delete them.

**Nothing the printer writes identifies the job.** No print id, no model name, no
session marker — only filenames and mtimes. So sessions are inferred, and the
inference has exactly one reliable signal.

### A short segment ends a print — the only physical signal

The chamber recording rotates at a **fixed size**. Every full segment is within
0.1% of every other, so a segment that comes in short was closed early, which
means the recording stopped, which means the print stopped. Measured over 61
real segments:

```
full segments   240.2 - 240.4 MB    (100% of modal, every single one)
print endings    29.6  72.2  112.2  160.8  190.5  218.5 MB
                 12%   30%   47%    67%    79%    91%
nothing at all   between 92% and 99%
```

The distribution is effectively binary, which is what makes a 95% threshold safe
rather than tuned. The rotation size is learned from the ledger's median — it is
a property of the printer, not something to ask the user for, and the median is
robust to the short segments being measured against.

This is the only boundary signal that is **physical rather than inferential**,
and the only one that works at all when the timelapse went to internal storage.

### The timelapse also ends a session; the gap is only a fallback

`/timelapse/video_<ts>.mp4` is written **once, when a print ends**. That is the
boundary. It is marked `ends_session = true` in the rule table, and the next file
after it opens a new session regardless of timing.

Time alone cannot do this job, and the data says so plainly. A failed print and
the redo that replaced it were **26 minutes** apart, while gaps *within* a single
print reach **18 minutes**. Splitting on time would need a ~22-minute threshold —
four minutes above normal — and any print with a slow layer would fragment across
two folders. The first version of this feature used a 45-minute gap and duly
merged the failed print with its redo, putting a 0.1 MB timelapse from a
cancelled job at the root of a 4-hour print that had none of its own.

`session_gap_minutes` remains as the fallback for prints that produce no
timelapse, and stays generous (45) on purpose: merging two prints is a nuisance,
fragmenting one is worse.

**The known limit, stated rather than hidden:** a print that produces no
timelapse, followed soon after by another, will merge. Nothing in the data
distinguishes them.

### Two ordering details that matter

- **Ties put the closer last.** The truncated final segment and the timelapse are
  flushed in the same second. If the closer sorted first, that segment would open
  a new session and end up alone in its own folder.
- **Candidates are drained in mtime order**, not path order, because session
  boundaries are chronological and a backlog is drained all at once long after
  the fact.

### The teardown is one flush, and it is not the sort order that saves it

exFAT keeps mtimes to 10 ms, so "the same second" was never quite the case.
The real end of a print, from the ledger:

```
00:24:52.62   video_….jpg          the timelapse's thumbnail
00:24:52.72   ipcam-record.64.mp4  17.8 MB — short, a closer
00:24:52.75   video_…_mini.jpg     the timelapse's small thumbnail
00:24:52.80   video_….mp4          the timelapse — a closer
```

Sorted by mtime the segment closes the print, and the three files behind it
open a new session — a folder holding a timelapse and a thumbnail and nothing
else, beside a 4.4-hour print with no timelapse in it. The tie-break above
cannot help, because these are not ties. And it gets worse: with the printer's
timelapse filed elsewhere, the print reads as having none, and the Pi spends
thirteen minutes rendering a reconstruction it did not need.

`TEARDOWN_SECONDS` (30) is the rule: anything landing that soon after the
session's latest closer is part of the flush and joins the print it ended. It
is measured from the latest **closer**, not the latest file — once the `_mini`
thumbnail has joined, the last file is no longer a closer, and measuring from it
would let the timelapse open a folder of its own again. A sliced file inside the
window still opens a new print; pressing print is pressing print.

**This was declared in 0.6.0 at 120 s and applied only in the archive
migration.** The daemon defined the constant and never read it, and the 0.6.0
retro recorded it as landed in both. Every print with a timelapse since then got
the one-file folder. It is 30 s now rather than 120 because the window is no
longer closers-only — the closest redo on record started 141 s after the short
segment that ended the failed attempt, and its first file is a thumbnail.

### The recording can beat the sliced file

"The sliced file lands ~15 minutes before the first segment" held for the print
that motivated `starts_session`, and not for the two after it:

```
19:39:20  ipcam-record.42.jpg               the chamber recording's first thumbnail
19:39:22  07 Vault Door_plate_1.gcode.3mf   the sliced file
```

A job started from Handy begins recording at once. Sorted by mtime the
thumbnail opened `2026-09-15_1939`, the sliced file opened
`2026-09-15_1939_07_Vault_Door_plate_1` two seconds later, and the thumbnail
was held six hours as an unfinished print before shipping as a one-file folder.

`START_SKEW_SECONDS` (120): a sliced file whose predecessor is a **nameless,
still-open** session that **opened** within the window takes that session over
— `Drainer._rename_session` moves the staged files and repoints the ledger rows,
per file, move first and row second, so a crash mid-way leaves every row
pointing at wherever its file actually is. It keys on when the session opened,
not on its last file, so a print that has been recording for an hour when the
next job is queued is never mistaken for that job's own recording.

### Manually exported timelapses cannot be grouped by time

If you copy timelapses from the printer's internal storage onto the USB drive
through its UI, **every exported file gets the mtime of the copy**. Six files
exported in one go arrive stamped with the same second, so they land in a single
session named for the moment you pressed the button rather than for any print.

The real timestamp survives only in the filename — `video_2026-09-02_21-51-18.mp4`
— and the printer's clock is ~12 hours off, so it orders correctly and lies about
absolute time. Parsing it is possible and not currently done.

The practical advice is simpler: **export one print's timelapse at a time**, and
it lands in its own session.

### Session names

`<model>_MM_DD_YY` — `Voronoi_Classic_Mustang_3D_Printable_Car_Model_09_23_26`
— or a bare `MM_DD_YY` when the sliced file carried a preset name rather than
a model's. Model first, because Finder is scanned by name; short date second,
because it is what you check once the name matches. No time of day (0.9.6): it
prefixed every folder with fourteen characters nobody read, and Finder sorted
the archive by when a print happened rather than what it was. Everything up to
0.9.5 was named `YYYY-MM-DD_HHMM[_model]`, and the folders already on the Mac
keep those names — nothing is renamed in place.

Day-granular names make a same-day repeat ordinary: A, then B, then A again is
a normal afternoon. `_distinct` suffixes the repeat (`A_09_23_26-2`) and checks
against **every session the ledger has ever named**, not only the one before
it. The minute-granular version compared against its predecessor alone, which
was fine when a collision needed two prints in one minute; with B in between,
it would have merged the second A into the first.

The date is the first file that opened the session — and taken from the
**mtime**, not the filename. The P2S's clock is 12 hours ahead (it keeps
UTC+8, against EDT), so its filenames say `21-13` for a print that ran at
`09:13`. The folder names are right; the names inside them are not.

A redo — the printer re-running a job after a failure or a cancel, with no new
sliced file — keeps a plain stamp, so the 24-minute failed attempt carries the
model name and the 4.4-hour print that replaced it does not. Naming the redo
after the closed session before it would be a guess: a print started from the
printer's own storage produces no sliced file either. Not done.

Minute granularity means two sessions starting in the same minute would collide,
so `_distinct()` suffixes `-2`, `-3`. Unlikely, but a collision silently merges
two prints, which is the thing this whole section exists to prevent.

## The health verdict is an alarm, so it must not move

`health.verdict()` returns one line — `ok`, or `PROBLEM: …`. RIA consumes it as
a `watch` job, which notifies **when the string changes**. That makes the
verdict an alarm rather than a report, and it carries an unusual constraint:

> Nothing that moves on its own may appear in it.

Not file counts, not bytes archived, not idle seconds, and — the one that got
through — **not how long the problem has been going on**. `problems()` rendered
`data integrity event {N}m ago`, so a single incident emitted a different string
every minute, and RIA read each one as a new problem to report. The Mac-side
reader had the identical bug on its staleness branch and texted every fifteen
minutes, overnight, about one unreachable Pi (v0.9.3, RIA v2.41.5).

**Why the tests missed it.** Verdict stability was tested — on the healthy path.
That turns out to prove nothing. A `watch` checker is only ever compared against
itself, so a constant `ok` says nothing about the string emitted once something
is wrong, and the wrong-path string is the only one anybody is ever woken by.
The tests now pin a *problem* verdict as byte-identical at 2 minutes and at 30.

Elapsed time is not lost — it stays in the status payload and in the human
output, both of which are read on demand and neither of which is a change
detector. A number is fine in a report and dangerous in an alarm.

## Known gaps

- **The 4 GB question is open.** exFAT is the default and has no per-file limit.
  If the P2S turns out to mount FAT32 only, a timelapse over 4 GB will be
  truncated by the printer before the drainer ever sees it, and no amount of
  code here can recover it. Needs one long print to settle.
- **Nothing alerts actively yet.** `status.json` is written and `bambu-drain
  status` reports `staging_pct`, but nothing pushes. Feeding this to RIA (which
  already runs 24/7 on the Mac and can text) is the obvious next step.
- **Untested against the printer.** The full software path is now proven on the
  real Pi 4: gadget created and bound to `fe980000.usb`, a root-level file and a
  nested file drained off the image, shipped to iCloud, and verified identical
  by SHA-256 on both ends. What remains unproven is the printer itself — whether
  it mounts the exFAT image, and whether its write pattern trips the idle gate
  in a way a synthetic test does not.

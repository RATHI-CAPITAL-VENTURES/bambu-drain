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
stays enumerated and only the media goes away. Unbinding the UDC would make the
whole device vanish and re-enumerate, which is a much ruder thing to do to a
machine that may be mid-job.

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

## Ledger

`ledger.db` (SQLite, WAL) keys on the file's SHA-256. `known()` gates every
delete. A pass that dies between "copied" and "deleted" re-runs and simply
deletes; a pass that dies between "uploaded" and "verified" keeps the staging
copy and retries.

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

## Two environment traps, found on real hardware

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

Related: point it at the Mac's mDNS `.local` name, not its IP. The Mac's
address moved during this project's own setup.

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

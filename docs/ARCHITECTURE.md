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

## Ledger

`ledger.db` (SQLite, WAL) keys on the file's SHA-256. `known()` gates every
delete. A pass that dies between "copied" and "deleted" re-runs and simply
deletes; a pass that dies between "uploaded" and "verified" keeps the staging
copy and retries.

## Staging budget

`staging_max_gb` blocks the drain loop when the Pi is filling up. Without it, a
broken ship loop would trade the printer's full disk for the Pi's — a strictly
worse failure, because the Pi is also the thing that fixes it.

## Known gaps

- **The 4 GB question is open.** exFAT is the default and has no per-file limit.
  If the P2S turns out to mount FAT32 only, a timelapse over 4 GB will be
  truncated by the printer before the drainer ever sees it, and no amount of
  code here can recover it. Needs one long print to settle.
- **Nothing alerts actively yet.** `status.json` is written and `bambu-drain
  status` reports `staging_pct`, but nothing pushes. Feeding this to RIA (which
  already runs 24/7 on the Mac and can text) is the obvious next step.
- **Untested against real hardware.** Every pure-logic path has tests; the
  configfs and mount paths cannot be exercised without the Pi wired to the
  printer. `drain --once --dry-run` exists for exactly that first run.

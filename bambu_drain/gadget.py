"""USB mass-storage gadget control via configfs.

We use configfs rather than the legacy `g_mass_storage` module for one reason:
it lets us change the *medium* without tearing down the *device*. Writing an
empty string to `lun.0/file` is exactly a card reader with the card pulled out —
the printer keeps seeing a USB drive attached, it just reports no media for a
few seconds. Unbinding the UDC instead makes the whole device vanish and
re-enumerate, which is ruder — so it is done only when it has to be: after a
pass that changed the filesystem, because nothing less makes the printer
re-read it. See `cycle_in`.

Nothing in here is safe to run while the printer is writing. The caller owns
that decision; see drain.py.
"""

from __future__ import annotations

import errno
import os
import time
from pathlib import Path

CONFIGFS_ROOT = Path("/sys/kernel/config/usb_gadget")

# Linux Foundation / Multifunction Composite Gadget. For mass storage the host
# cares about the SCSI INQUIRY strings, not these, so they stay fixed.
ID_VENDOR = "0x1d6b"
ID_PRODUCT = "0x0104"


class GadgetError(RuntimeError):
    pass


def _write(path: Path, value: str) -> None:
    try:
        path.write_text(value)
    except OSError as exc:
        raise GadgetError(f"write {value!r} -> {path}: {exc}") from exc


class MassStorageGadget:
    """One mass-storage gadget with a single LUN."""

    def __init__(self, cfg, configfs_root: Path = CONFIGFS_ROOT):
        self.cfg = cfg
        self.root = Path(configfs_root) / cfg.name
        self.func = self.root / "functions" / "mass_storage.usb0"
        self.lun = self.func / "lun.0"

    # -- lifecycle ---------------------------------------------------------

    @property
    def exists(self) -> bool:
        return self.root.is_dir()

    def create(self) -> None:
        """Build the gadget tree. Idempotent."""
        if self.exists:
            return

        strings = self.root / "strings" / "0x409"
        config = self.root / "configs" / "c.1"
        cfg_strings = config / "strings" / "0x409"

        for d in (self.root, strings, config, cfg_strings, self.func):
            d.mkdir(parents=True, exist_ok=True)

        _write(self.root / "idVendor", ID_VENDOR)
        _write(self.root / "idProduct", ID_PRODUCT)
        _write(self.root / "bcdDevice", "0x0100")
        _write(self.root / "bcdUSB", "0x0200")

        _write(strings / "serialnumber", self.cfg.serial)
        _write(strings / "manufacturer", self.cfg.vendor)
        _write(strings / "product", self.cfg.product)
        _write(cfg_strings / "configuration", "mass storage")
        _write(config / "MaxPower", "250")

        # removable=1 is what makes eject/insert legal at all. A non-removable
        # LUN that loses its medium is a hardware fault as far as the host is
        # concerned.
        _write(self.lun / "removable", "1")
        _write(self.lun / "ro", "0")
        _write(self.lun / "cdrom", "0")
        # nofua=1: honour the host's cache-flush semantics rather than forcing
        # a sync on every write. The printer streams video; FUA on every block
        # would be brutal on the SD card.
        _write(self.lun / "nofua", "1")

        inquiry = self.lun / "inquiry_string"
        if inquiry.exists():
            # "vendor(8) product(16) revision(4)", space padded.
            _write(inquiry, f"{self.cfg.vendor:<8}{self.cfg.product:<16}{'0001':<4}")

        link = config / "mass_storage.usb0"
        if not link.exists():
            os.symlink(self.func, link)

        self.insert()

    def destroy(self) -> None:
        """Tear the gadget down. Best-effort; configfs removal order is fussy."""
        if not self.exists:
            return
        self.unbind()
        self.eject()

        link = self.root / "configs" / "c.1" / "mass_storage.usb0"
        if link.is_symlink():
            link.unlink()
        for d in (
            self.root / "configs" / "c.1" / "strings" / "0x409",
            self.root / "configs" / "c.1",
            self.func,
            self.root / "strings" / "0x409",
            self.root,
        ):
            try:
                d.rmdir()
            except OSError:
                pass

    # -- binding -----------------------------------------------------------

    def available_udc(self) -> str:
        if self.cfg.udc:
            return self.cfg.udc
        udcs = sorted(p.name for p in Path("/sys/class/udc").iterdir())
        if not udcs:
            raise GadgetError(
                "no UDC found. On a Pi 4 this means dwc2 is not loaded in "
                "peripheral mode — check dtoverlay=dwc2 in config.txt and "
                "modules-load=dwc2 in cmdline.txt."
            )
        return udcs[0]

    @property
    def bound(self) -> bool:
        udc = self.root / "UDC"
        return udc.exists() and udc.read_text().strip() != ""

    def bind(self) -> None:
        if not self.bound:
            _write(self.root / "UDC", self.available_udc())

    def unbind(self) -> None:
        if self.bound:
            _write(self.root / "UDC", "\n")

    # -- media -------------------------------------------------------------

    @property
    def media_present(self) -> bool:
        f = self.lun / "file"
        return f.exists() and f.read_text().strip() != ""

    def eject(self, force: bool = True) -> None:
        """Detach the backing image so the Pi can mount it safely.

        The host must not hold the medium open. `forced_eject` (Linux >= 5.15)
        makes that non-negotiable; without it the kernel returns EBUSY and we
        surface that rather than pretending it worked, because the alternative
        is mounting a filesystem the printer is still writing to.
        """
        if not self.media_present:
            return

        forced = self.lun / "forced_eject"
        if force and forced.exists():
            _write(forced, "1")
            return

        try:
            (self.lun / "file").write_text("\n")
        except OSError as exc:
            if exc.errno == errno.EBUSY:
                raise GadgetError(
                    "printer still holds the medium open and this kernel has no "
                    "forced_eject; refusing to mount underneath it"
                ) from exc
            raise GadgetError(f"eject: {exc}") from exc

    def insert(self) -> None:
        _write(self.lun / "file", str(self.cfg.image))

    def cycle_out(self, settle_seconds: float = 1.0) -> None:
        self.eject()
        # Give the host a moment to notice the media change before we touch the
        # image. Ejecting and immediately mounting has raced on slower hosts.
        time.sleep(settle_seconds)

    def cycle_in(self, reconnect: bool = False, settle_seconds: float = 1.0,
                 attach_timeout: float = 5.0) -> None:
        """Hand the medium back — with `reconnect`, as a newly plugged-in drive.

        A media change alone does NOT make the printer re-read the filesystem.
        It keeps the allocation bitmap it loaded when it first mounted the
        drive, so space the Pi frees is invisible to it, and it writes that
        stale bitmap back over ours the next time it allocates. Measured on
        2026-10-02: 9.2 GB marked used on a stick holding 3 MB of files, every
        orphaned run a recording drained weeks earlier, and a printer refusing
        to record for "not enough storage" with 23 GB free on disk.

        Only a disconnect makes it mount afresh, so a pass that changed the
        filesystem detaches the whole device while the medium goes back in.
        A pass that changed nothing keeps the gentler media change.
        """
        if not reconnect:
            self.insert()
            return
        try:
            self.unbind()
        finally:
            # Whatever happened to the unbind, the medium goes back.
            self.insert()
        time.sleep(settle_seconds)
        self.bind()
        # Enumeration takes a moment, and a status snapshot taken inside it
        # reads "not attached" — a false alarm after every real drain.
        deadline = time.monotonic() + attach_timeout
        while time.monotonic() < deadline and self.host_state() != "configured":
            time.sleep(0.2)

    def host_state(self) -> str | None:
        """What the USB controller thinks: `configured` once a host has us."""
        try:
            state = Path("/sys/class/udc") / self.available_udc() / "state"
            return state.read_text().strip()
        except (OSError, GadgetError):
            return None

    # -- host activity -----------------------------------------------------

    def last_host_write(self) -> float:
        """mtime of the backing image.

        This is the whole idle signal. The gadget driver writes through to the
        backing file with ordinary VFS calls, so any SCSI WRITE the printer
        issues moves this forward. It needs no network, no MQTT, and no LAN
        mode — which is precisely why this design keeps Bambu cloud intact.
        """
        return self.cfg.image.stat().st_mtime

    def idle_seconds(self) -> float:
        return time.time() - self.last_host_write()

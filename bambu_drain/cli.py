from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from pathlib import Path

from . import __version__, config, health
from .drain import Drainer
from .gadget import GadgetError, MassStorageGadget
from .ledger import Ledger
from .ship import Shipper


def _wire(args):
    cfg = config.load(args.config)
    ledger = Ledger(cfg.ledger_path)
    gadget = MassStorageGadget(cfg.gadget)
    return cfg, ledger, gadget, Drainer(cfg, ledger, gadget), Shipper(cfg, ledger)


def cmd_gadget(args) -> int:
    cfg, ledger, gadget, _, _ = _wire(args)
    try:
        if args.action == "create":
            if not cfg.gadget.image.exists():
                print(f"backing image missing: {cfg.gadget.image}", file=sys.stderr)
                print("run setup/02-create-image.sh first", file=sys.stderr)
                return 1
            gadget.create()
            gadget.bind()
            print(f"gadget {cfg.gadget.name} bound to {gadget.available_udc()}")
        elif args.action == "destroy":
            gadget.destroy()
            print("gadget destroyed")
        elif args.action == "eject":
            gadget.eject()
            print("media ejected")
        elif args.action == "insert":
            gadget.insert()
            print("media inserted")
    except GadgetError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


def cmd_drain(args) -> int:
    cfg, ledger, gadget, drainer, _ = _wire(args)
    if args.once:
        print(json.dumps(drainer.run_once(dry_run=args.dry_run), indent=2))
        return 0
    logging.info("drain loop started (idle gate %.1f min)", cfg.drain.idle_minutes)
    while True:
        try:
            drainer.run_once(dry_run=args.dry_run)
        except Exception:
            logging.exception("drain pass failed")
            ledger.event("drain_error", "see journal")
        time.sleep(cfg.drain.poll_seconds)


def cmd_ship(args) -> int:
    cfg, ledger, _, _, shipper = _wire(args)
    if args.once:
        print(json.dumps(shipper.run_once(), indent=2))
        return 0
    logging.info("ship loop started -> %s", cfg.ship.host)
    while True:
        try:
            shipper.run_once()
            shipper.prune_remote()
        except Exception:
            logging.exception("ship pass failed")
            ledger.event("ship_error", "see journal")
        time.sleep(cfg.ship.poll_seconds)


def cmd_status(args) -> int:
    cfg, ledger, gadget, drainer, _ = _wire(args)
    payload = health.snapshot(cfg, ledger, gadget, drainer)
    health.write(cfg, payload)
    if args.json:
        print(json.dumps(payload, indent=2))
        return 0

    g, d, a = payload["gadget"], payload["drain"], payload["archive"]
    print(f"gadget      {'bound' if g['bound'] else 'NOT BOUND'}, "
          f"media {'in' if g['media_present'] else 'OUT'}, "
          f"idle {g['idle_seconds']}s")
    print(f"drain       {d['blocked_reason'] or 'ready'}")
    print(f"staging     {d['staging_bytes'] / 1024**3:.2f} GB "
          f"({d['staging_pct']}% of budget)")
    print(f"archived    {a['files_total']} files, {a['bytes_total'] / 1024**3:.2f} GB")
    print(f"pending     {a['files_pending_ship']} files, "
          f"{a['bytes_pending_ship'] / 1024**3:.2f} GB not yet on the Mac")
    return 0


def cmd_doctor(args) -> int:
    """Check the things that are wrong on a fresh Pi, in the order they bite."""
    cfg, ledger, gadget, _, shipper = _wire(args)
    ok = True

    def check(label: str, good: bool, hint: str = "") -> None:
        nonlocal ok
        print(f"[{'ok ' if good else 'FAIL'}] {label}")
        if not good:
            ok = False
            if hint:
                print(f"       {hint}")

    check("running as root", Path("/proc/self").exists() and __import__("os").geteuid() == 0,
          "configfs and mount both need root")
    check("configfs mounted", Path("/sys/kernel/config").is_dir(),
          "modprobe configfs; mount -t configfs none /sys/kernel/config")
    check("libcomposite loaded", Path("/sys/kernel/config/usb_gadget").is_dir(),
          "modprobe libcomposite")
    udcs = list(Path("/sys/class/udc").iterdir()) if Path("/sys/class/udc").is_dir() else []
    check("UDC present (dwc2 in peripheral mode)", bool(udcs),
          "add dtoverlay=dwc2 to /boot/firmware/config.txt and "
          "modules-load=dwc2 to cmdline.txt, then reboot")
    check(f"backing image {cfg.gadget.image}", cfg.gadget.image.exists(),
          "run setup/02-create-image.sh")
    check("gadget created", gadget.exists, "bambu-drain gadget create")
    check(f"ssh to {cfg.ship.host}", shipper.reachable(),
          f"ssh-copy-id -i {cfg.ship.ssh_key}.pub {cfg.ship.host}")
    return 0 if ok else 1


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="bambu-drain", description=__doc__)
    p.add_argument("--config", default=config.DEFAULT_PATH, type=Path)
    p.add_argument("--version", action="version", version=__version__)
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)

    g = sub.add_parser("gadget", help="create/destroy/eject/insert the USB gadget")
    g.add_argument("action", choices=["create", "destroy", "eject", "insert"])
    g.set_defaults(func=cmd_gadget)

    d = sub.add_parser("drain", help="move files off the stick onto the Pi")
    d.add_argument("--once", action="store_true")
    d.add_argument("--dry-run", action="store_true",
                   help="report what would move; delete nothing")
    d.set_defaults(func=cmd_drain)

    s = sub.add_parser("ship", help="move files from the Pi to the Mac")
    s.add_argument("--once", action="store_true")
    s.set_defaults(func=cmd_ship)

    st = sub.add_parser("status", help="what is happening right now")
    st.add_argument("--json", action="store_true")
    st.set_defaults(func=cmd_status)

    doc = sub.add_parser("doctor", help="check the setup, in the order it breaks")
    doc.set_defaults(func=cmd_doctor)

    args = p.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())

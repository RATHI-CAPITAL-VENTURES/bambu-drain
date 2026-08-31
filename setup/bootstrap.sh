#!/usr/bin/env bash
# One privileged entry point, so the whole install is a single sudo prompt.
#
#   sudo setup/bootstrap.sh prepare   # boot config + image + install (no services)
#   sudo setup/bootstrap.sh verify    # create the gadget, run doctor, dry-run drain
#   sudo setup/bootstrap.sh start     # enable and start the three services
#
# `prepare` requires a reboot afterwards: the USB-C port does not become a
# peripheral until the dwc2 overlay is applied at boot, and /sys/class/udc stays
# empty until it is.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
REPO="$(dirname "$HERE")"
SIZE_GB="${SIZE_GB:-32}"
FS="${FS:-exfat}"

[ "$(id -u)" -eq 0 ] || { echo "run with sudo" >&2; exit 1; }

step() { printf '\n\033[1m== %s\033[0m\n' "$*"; }

cmd_prepare() {
  step "1/3  USB peripheral mode"
  "$HERE/01-enable-dwc2.sh"

  step "2/3  backing image (${SIZE_GB}G, $FS)"
  if [ -e /srv/bambu-drain/stick.img ]; then
    echo "· /srv/bambu-drain/stick.img already exists, leaving it alone"
  else
    "$HERE/02-create-image.sh" "$SIZE_GB" "$FS"
  fi

  step "3/3  install package, config and units"
  "$HERE/03-install.sh"
  # Deliberately NOT enabled yet — nothing starts draining until a dry run has
  # been eyeballed. `bootstrap.sh start` does that.
  systemctl disable bambu-drain-gadget.service bambu-drain.service \
                    bambu-drain-ship.service >/dev/null 2>&1 || true

  cat <<'MSG'

Prepared. REBOOT NOW:

    sudo reboot

After it comes back, /sys/class/udc must be non-empty. If it is empty, the
dwc2 overlay did not take and nothing else will work.
MSG
}

cmd_verify() {
  step "UDC present?"
  if [ -z "$(ls /sys/class/udc 2>/dev/null)" ]; then
    echo "FAIL: /sys/class/udc is empty — dwc2 is not in peripheral mode." >&2
    echo "Check 'dtoverlay=dwc2,dr_mode=peripheral' in /boot/firmware/config.txt" >&2
    exit 1
  fi
  ls /sys/class/udc

  step "create and bind the gadget"
  modprobe libcomposite || true
  PYTHONPATH=/opt/bambu-drain python3 -m bambu_drain gadget create

  step "doctor"
  PYTHONPATH=/opt/bambu-drain python3 -m bambu_drain doctor || true

  step "dry-run drain (deletes nothing)"
  PYTHONPATH=/opt/bambu-drain python3 -m bambu_drain drain --once --dry-run
}

cmd_start() {
  step "enable and start"
  systemctl enable --now bambu-drain-gadget.service
  systemctl enable --now bambu-drain.service
  systemctl enable --now bambu-drain-ship.service
  systemctl --no-pager --lines=0 status \
    bambu-drain-gadget bambu-drain bambu-drain-ship || true
}

case "${1:-}" in
  prepare) cmd_prepare ;;
  verify)  cmd_verify ;;
  start)   cmd_start ;;
  *) echo "usage: sudo $0 {prepare|verify|start}" >&2; exit 1 ;;
esac

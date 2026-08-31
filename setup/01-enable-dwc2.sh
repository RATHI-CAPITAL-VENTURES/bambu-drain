#!/usr/bin/env bash
# Put the Pi 4's USB-C port into USB peripheral mode.
#
# READ THIS BEFORE PLUGGING ANYTHING IN.
#
# On a Pi 4 Model B the USB-C port is BOTH the power input and the only port
# that can act as a USB device. Once this script has run, that port is a data
# port going to the printer — so the Pi must be powered some other way:
#
#   * 5V/3A supply wired to GPIO pin 2 or 4 (5V) and pin 6 (GND), and
#   * ideally a VBUS-blocking adapter on the USB-C line so the printer is not
#     back-feeding 5V into a Pi that is already powered.
#
# Powering a Pi 4 from a printer's USB-A port will brown out under load, and a
# brownout mid-write is exactly how the backing image gets corrupted.
set -euo pipefail

BOOT=/boot/firmware
[ -d "$BOOT" ] || BOOT=/boot          # pre-Bookworm layout
CONFIG="$BOOT/config.txt"
CMDLINE="$BOOT/cmdline.txt"

[ "$(id -u)" -eq 0 ] || { echo "run as root" >&2; exit 1; }
[ -f "$CONFIG" ] || { echo "no $CONFIG — is this a Raspberry Pi?" >&2; exit 1; }

changed=0

if ! grep -qE '^\s*dtoverlay=dwc2' "$CONFIG"; then
  printf '\n# bambu-drain: USB peripheral mode on the USB-C port\ndtoverlay=dwc2,dr_mode=peripheral\n' >> "$CONFIG"
  echo "· config.txt: added dtoverlay=dwc2,dr_mode=peripheral"
  changed=1
else
  echo "· config.txt: dwc2 overlay already present"
fi

if ! grep -q 'modules-load=dwc2' "$CMDLINE"; then
  # cmdline.txt must stay a single line; append to it rather than adding one.
  sed -i 's/\brootwait\b/rootwait modules-load=dwc2/' "$CMDLINE"
  grep -q 'modules-load=dwc2' "$CMDLINE" || sed -i '1s/$/ modules-load=dwc2/' "$CMDLINE"
  echo "· cmdline.txt: added modules-load=dwc2"
  changed=1
else
  echo "· cmdline.txt: dwc2 already loaded"
fi

if ! grep -qE '^\s*libcomposite\s*$' /etc/modules; then
  echo libcomposite >> /etc/modules
  echo "· /etc/modules: added libcomposite"
  changed=1
fi

# exFAT support. Mainline kernels have the driver; exfatprogs supplies mkfs.
if ! command -v mkfs.exfat >/dev/null 2>&1; then
  echo "· installing exfatprogs"
  apt-get update -qq && apt-get install -y -qq exfatprogs
fi

if [ "$changed" -eq 1 ]; then
  echo
  echo "Reboot required. After reboot, /sys/class/udc should be non-empty:"
  echo "  ls /sys/class/udc"
  echo "If it is empty, dwc2 did not come up in peripheral mode."
else
  echo
  echo "No changes needed."
fi

#!/usr/bin/env bash
# Create and format the backing image the printer will see as a USB stick.
#
#   ./02-create-image.sh [size_gb] [exfat|fat32]
#
# On filesystem choice: exFAT has no 4 GB per-file limit and is the right answer
# if the P2S mounts it. FAT32 is the safer bet for compatibility but WILL
# truncate a timelapse that grows past 4 GB. If you are unsure, format FAT32,
# run one long print, and check the video plays to the end.
set -euo pipefail

SIZE_GB="${1:-32}"
FS="${2:-exfat}"
IMAGE="${IMAGE:-/srv/bambu-drain/stick.img}"

[ "$(id -u)" -eq 0 ] || { echo "run as root" >&2; exit 1; }

case "$FS" in
  exfat|fat32) ;;
  *) echo "filesystem must be exfat or fat32" >&2; exit 1 ;;
esac

mkdir -p "$(dirname "$IMAGE")"

if [ -e "$IMAGE" ]; then
  echo "refusing to overwrite existing $IMAGE" >&2
  echo "move it aside first if you really mean to reformat" >&2
  exit 1
fi

# Sparse: the file reports SIZE_GB to the printer but only occupies what is
# actually written. Keep an eye on real disk usage with `du -h --apparent-size`
# versus `du -h`.
echo "· allocating ${SIZE_GB}G sparse image at $IMAGE"
truncate -s "${SIZE_GB}G" "$IMAGE"

echo "· formatting $FS"
if [ "$FS" = exfat ]; then
  mkfs.exfat -n BAMBU "$IMAGE" >/dev/null
else
  mkfs.vfat -F 32 -n BAMBU "$IMAGE" >/dev/null
fi

chmod 600 "$IMAGE"
echo
echo "Done. $IMAGE — ${SIZE_GB}G, $FS"
echo "Set gadget.size_gb and gadget.fs in /etc/bambu-drain/config.toml to match."

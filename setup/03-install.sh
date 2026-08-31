#!/usr/bin/env bash
# Install bambu-drain and its two services.
set -euo pipefail

SRC="$(cd "$(dirname "$0")/.." && pwd)"
[ "$(id -u)" -eq 0 ] || { echo "run as root" >&2; exit 1; }

echo "· installing package to /opt/bambu-drain"
mkdir -p /opt/bambu-drain
cp -r "$SRC/bambu_drain" /opt/bambu-drain/

cat > /usr/local/bin/bambu-drain <<'SH'
#!/bin/sh
exec python3 -m bambu_drain "$@"
SH
chmod +x /usr/local/bin/bambu-drain
echo 'PYTHONPATH=/opt/bambu-drain' > /etc/default/bambu-drain

mkdir -p /etc/bambu-drain /srv/bambu-drain/staging /mnt/bambu-stick
if [ ! -f /etc/bambu-drain/config.toml ]; then
  cp "$SRC/config.example.toml" /etc/bambu-drain/config.toml
  echo "· wrote /etc/bambu-drain/config.toml — EDIT IT (ship.host, ship.dest)"
else
  echo "· /etc/bambu-drain/config.toml exists, leaving it alone"
fi

echo "· installing systemd units"
cp "$SRC"/deploy/*.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable bambu-drain-gadget.service bambu-drain.service bambu-drain-ship.service

echo
echo "Next:"
echo "  1. edit /etc/bambu-drain/config.toml"
echo "  2. bambu-drain doctor"
echo "  3. systemctl start bambu-drain-gadget bambu-drain bambu-drain-ship"

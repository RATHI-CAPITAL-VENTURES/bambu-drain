#!/usr/bin/env bash
#
# Put the Pi on your tailnet so it stops depending on a shared LAN.
#
#   ./setup/04-tailscale.sh <pi-ssh-alias> <mac-tailnet-name> [mac-alias]
#   ./setup/04-tailscale.sh rpi ishans-m2-macbook-pro ishan-mac
#
# RUN THIS FROM THE MAC, WHILE BOTH MACHINES ARE STILL ON THE SAME NETWORK.
# It needs SSH to the Pi, which is the thing it is about to make unnecessary.
#
# WHY
#
# Both directions of this system were pinned to a LAN address that moves.
# The Pi shipped to the Mac's mDNS name, whose cache went stale three times in
# two days when the Mac's DHCP lease changed — surfacing as "no route to host",
# which reads like the Mac being switched off. And the Mac reached the Pi at a
# hardcoded 192.168.x.x, which broke the moment the Mac joined another network.
#
# Tailscale gives both machines a name and a 100.x address that do not change,
# work from anywhere, and are encrypted end to end.
set -euo pipefail

if [ $# -lt 2 ]; then
  echo "usage: $0 <pi-ssh-alias> <mac-tailnet-name> [mac-alias]" >&2
  echo "   eg: $0 rpi ishans-m2-macbook-pro ishan-mac" >&2
  exit 1
fi
PI="$1"
MAC_TS="$2"
MAC_ALIAS="${3:-ishan-mac}"

say() { printf '\033[1m·\033[0m %s\n' "$*"; }

ssh -o BatchMode=yes -o ConnectTimeout=8 "$PI" true 2>/dev/null || {
  echo "error: cannot ssh to '$PI'." >&2
  echo "       Run this from the same network as the Pi — it is the last time" >&2
  echo "       you will need to be." >&2
  exit 1
}
PI_USER="$(ssh -n "$PI" 'whoami')"

say "installing tailscale on the Pi"
ssh -n "$PI" 'command -v tailscale >/dev/null || (curl -fsSL https://tailscale.com/install.sh | sudo sh)'

say "bringing it up — AUTHENTICATE IN THE BROWSER WHEN THE URL APPEARS"
echo
# Not -n: this one is interactive, and the auth URL must reach your eyes.
ssh -t "$PI" 'sudo tailscale up --ssh --hostname=bambu-drain-pi' || {
  echo "error: 'tailscale up' did not complete." >&2; exit 1; }
echo

PI_TS="$(ssh -n "$PI" 'tailscale status --json' | python3 -c '
import json,sys
print(json.load(sys.stdin)["Self"]["DNSName"].rstrip("."))')"
PI_IP="$(ssh -n "$PI" "tailscale ip -4" | head -1)"
say "Pi is on the tailnet as $PI_TS ($PI_IP)"

say "pointing the Pi at the Mac's TAILNET name (was mDNS, which moved)"
ssh -n "$PI" "sudo tee /etc/ssh/ssh_config.d/10-bambu-drain.conf >/dev/null" <<CONF
# The ship loop runs as root, so this must be system-wide — a Host block in a
# user's ~/.ssh/config is invisible to it.
#
# A TAILNET name, deliberately. The mDNS name resolved to a DHCP lease that
# moved three times in two days, and the stale cache read as "no route to host".
Host ${MAC_ALIAS}
  HostName ${MAC_TS}
  User $(whoami)
  IdentityFile /home/${PI_USER}/.ssh/id_ed25519
  IdentitiesOnly yes
  StrictHostKeyChecking accept-new
CONF
ssh -n "$PI" "sudo chmod 644 /etc/ssh/ssh_config.d/10-bambu-drain.conf"

say "pointing the Mac at the Pi's tailnet name"
python3 - "$PI" "$PI_TS" "$PI_USER" <<'PY'
import re, sys
from pathlib import Path
alias, ts_name, pi_user = sys.argv[1], sys.argv[2], sys.argv[3]
cfg = Path.home() / ".ssh" / "config"
text = cfg.read_text() if cfg.exists() else ""
block = (f"\n# bambu-drain: the Pi, over Tailscale. Not a LAN address — the Mac\n"
         f"# moves networks and the Pi does not follow.\n"
         f"Host {alias}\n  HostName {ts_name}\n  User {pi_user}\n"
         f"  IdentityFile ~/.ssh/id_ed25519\n  IdentitiesOnly yes\n"
         f"  ServerAliveInterval 30\n")
pat = re.compile(rf"^Host {re.escape(alias)}$.*?(?=^Host |\Z)", re.M | re.S)
text = pat.sub("", text).rstrip() + "\n" + block
cfg.write_text(text)
cfg.chmod(0o600)
print(f"  rewrote Host {alias} -> {ts_name}")
PY

say "verifying both directions over Tailscale"
ssh -o ConnectTimeout=15 "$PI" "true" && say "Mac -> Pi OK"
ssh -n "$PI" "sudo ssh -o BatchMode=yes -o ConnectTimeout=15 ${MAC_ALIAS} true" \
  && say "Pi -> Mac OK (as root, which is how the ship loop does it)" \
  || { echo "error: the Pi still cannot reach the Mac." >&2
       echo "       Is Tailscale running and signed in on the Mac?" >&2; exit 1; }

cat <<MSG

Done. Neither machine depends on the other's network any more.

  Mac  -> Pi   ssh ${PI}          (${PI_TS})
  Pi   -> Mac  ${MAC_ALIAS}       (${MAC_TS})

The Pi will keep draining the printer wherever it is, and ship whenever the
Mac is reachable — which is now "whenever the Mac is on", not "whenever the
Mac is on this Wi-Fi".
MSG

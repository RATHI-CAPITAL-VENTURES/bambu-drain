#!/usr/bin/env bash
#
# Run this ON THE MAC, once, after you can already `ssh <pi>` with a key.
#
#   setup/00-ssh-from-mac.sh <pi-ssh-alias> [mac-alias]
#   setup/00-ssh-from-mac.sh rpi ishan-mac
#
# It sets up the RETURN path — Pi to Mac — which is the half people forget,
# and it puts the Host alias where root can actually see it. The services run
# as root (they need configfs and mount), so an alias in the login user's
# ~/.ssh/config is invisible to them; `doctor` then reports the Mac as
# unreachable, which reads like a network fault and is not one.
set -euo pipefail

PI="${1:?usage: $0 <pi-ssh-alias> [mac-alias]}"
MAC_ALIAS="${2:-ishan-mac}"
MAC_HOST="$(scutil --get LocalHostName).local"
MAC_USER="$(whoami)"

say() { printf '\033[1m·\033[0m %s\n' "$*"; }

# The Mac's IP is a DHCP lease and will move — it moved during this project's
# own setup. mDNS is the stable handle.
say "Mac will be reached as ${MAC_USER}@${MAC_HOST} (mDNS, not an IP)"

if ! ssh -o BatchMode=yes -o ConnectTimeout=8 "$PI" true 2>/dev/null; then
  echo "error: cannot ssh to '$PI' with a key yet." >&2
  echo "       Run: ssh-copy-id -i ~/.ssh/id_ed25519.pub $PI" >&2
  exit 1
fi
say "Mac -> Pi works"

if ! sudo -n launchctl print system/com.openssh.sshd >/dev/null 2>&1; then
  # Not authoritative without sudo; just warn rather than block.
  say "NOTE: if the Pi cannot reach back, enable Remote Login on the Mac"
  say "      (System Settings > General > Sharing > Remote Login)"
fi

say "generating the Pi's key if it has none"
ssh "$PI" 'test -f ~/.ssh/id_ed25519 || ssh-keygen -t ed25519 -N "" -C "bambu-drain@$(hostname)" -f ~/.ssh/id_ed25519 >/dev/null'
PIKEY="$(ssh "$PI" 'cat ~/.ssh/id_ed25519.pub')"

mkdir -p ~/.ssh && chmod 700 ~/.ssh
touch ~/.ssh/authorized_keys && chmod 600 ~/.ssh/authorized_keys
if grep -qF "$PIKEY" ~/.ssh/authorized_keys; then
  say "Pi's key already authorised on the Mac"
else
  printf '%s\n' "$PIKEY" >> ~/.ssh/authorized_keys
  say "authorised the Pi's key on the Mac"
fi

PI_USER="$(ssh "$PI" 'whoami')"
say "writing /etc/ssh/ssh_config.d/10-bambu-drain.conf on the Pi (system-wide, so root sees it)"
# SC2087: the expansion is deliberately CLIENT side. The Mac's hostname, user
# and the Pi's username are all known here and meaningless on the Pi, so the
# heredoc must be interpolated before it is sent.
# shellcheck disable=SC2087
ssh "$PI" "sudo mkdir -p /etc/ssh/ssh_config.d && sudo tee /etc/ssh/ssh_config.d/10-bambu-drain.conf >/dev/null" <<CONF
# bambu-drain ships as root, so this must NOT live in a user's ~/.ssh/config.
Host ${MAC_ALIAS}
  HostName ${MAC_HOST}
  User ${MAC_USER}
  IdentityFile /home/${PI_USER}/.ssh/id_ed25519
  IdentitiesOnly yes
  StrictHostKeyChecking accept-new
CONF
ssh "$PI" "sudo chmod 644 /etc/ssh/ssh_config.d/10-bambu-drain.conf"

say "verifying Pi -> Mac AS ROOT (the way the services will do it)"
# SC2029: likewise deliberate — MAC_ALIAS is a Mac-side variable and we want
# its value in the remote command, not the name.
# shellcheck disable=SC2029
if ssh "$PI" "sudo ssh -o BatchMode=yes -o ConnectTimeout=10 ${MAC_ALIAS} true" 2>/dev/null; then
  say "OK — root on the Pi can reach the Mac"
else
  echo "FAILED: root on the Pi cannot reach ${MAC_ALIAS}." >&2
  echo "  - is Remote Login enabled on the Mac?" >&2
  echo "  - does ${MAC_HOST} resolve from the Pi? (getent hosts ${MAC_HOST})" >&2
  exit 1
fi

cat <<MSG

Done. Put this in /etc/bambu-drain/config.toml:

  [ship]
  host    = "${MAC_ALIAS}"
  dest    = "~/Library/Mobile Documents/com~apple~CloudDocs/BambuArchive"
  ssh_key = "/home/${PI_USER}/.ssh/id_ed25519"
MSG

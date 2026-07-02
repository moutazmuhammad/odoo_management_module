#!/usr/bin/env bash
#
# Recovery for harden_server.sh: put SSH back on port 22 with password login,
# so you can get back in. Run as root from the DigitalOcean web console.
#
set -u
[ "$(id -u)" -eq 0 ] || { echo "Run as root."; exit 1; }

log(){ printf '\n== %s ==\n' "$*"; }

# 1. Drop the hardening drop-ins that moved the port / disabled passwords.
log "Removing hardening SSH drop-ins"
rm -f /etc/ssh/sshd_config.d/00-hardening.conf
rm -f /etc/systemd/system/ssh.socket.d/port.conf

# 2. Restore the original sshd_config if a backup exists.
if [ -f /etc/ssh/sshd_config.bak.harden ]; then
  cp -f /etc/ssh/sshd_config.bak.harden /etc/ssh/sshd_config
  echo "  restored /etc/ssh/sshd_config from backup"
fi

# 3. Force port 22 + password login back on via a fresh drop-in (belt and braces).
mkdir -p /etc/ssh/sshd_config.d
cat > /etc/ssh/sshd_config.d/00-recover.conf <<'EOF'
Port 22
PasswordAuthentication yes
PermitRootLogin yes
EOF

# 4. Restore each user's authorized_keys backup so key login also works again.
while IFS=: read -r _ _ _ _ _ home _; do
  [ -n "$home" ] && [ -f "$home/.ssh/authorized_keys.bak.harden" ] || continue
  cp -f "$home/.ssh/authorized_keys.bak.harden" "$home/.ssh/authorized_keys"
  echo "  restored authorized_keys in $home"
done < /etc/passwd

# 5. Make sure no firewall is blocking SSH.
command -v ufw >/dev/null 2>&1 && ufw --force disable >/dev/null 2>&1 || true

# 6. Apply.
log "Restarting SSH on port 22"
systemctl daemon-reload
systemctl restart ssh.socket 2>/dev/null || true
systemctl restart ssh 2>/dev/null || systemctl restart sshd 2>/dev/null || true

sleep 1
echo "Listeners now:"
ss -ltn 2>/dev/null | grep -E ':(22|7812)\b' || echo "  (nothing on 22/7812 — check 'systemctl status ssh')"
echo "Done. Try:  ssh root@<server>"

#!/usr/bin/env bash
# ssh.socket was left in 'failed' state: a custom port.conf override
# (BindIPv6Only=both + 0.0.0.0 + [::]) caused an IPv4 bind collision. systemd's
# sshd-socket-generator already derives the correct 0.0.0.0:7812 + [::]:7812
# listeners from 'Port 7812' in sshd_config, so just remove the override.
# Run as root.
set -u
rm -f /etc/systemd/system/ssh.socket.d/port.conf
rmdir /etc/systemd/system/ssh.socket.d 2>/dev/null || true
systemctl daemon-reload
systemctl reset-failed ssh.socket ssh.service 2>/dev/null || true
systemctl stop ssh.service 2>/dev/null || true
pkill -x sshd 2>/dev/null || true        # clear any stale listener holding the port
sleep 1
systemctl enable --now ssh.socket 2>/dev/null || true
systemctl restart ssh.socket
sleep 1

MOD="$(find / -type d -name odoo_server_management 2>/dev/null | head -1)"
OUT="${MOD:-/tmp}/static/diag.txt"
{
  echo "=== ssh_fix3 @ $(date) ==="
  echo "=== listeners (want 0.0.0.0:7812 AND [::]:7812) ==="; ss -tlnp 2>/dev/null | grep -E ':7812 |:22 ' || echo "(none on 7812)"
  echo "=== ssh.socket is-active ==="; systemctl is-active ssh.socket
  echo "=== ssh.socket status ==="; systemctl status ssh.socket --no-pager 2>&1 | head -10
  echo "=== cat ssh.socket (effective) ==="; systemctl cat ssh.socket 2>/dev/null
  echo "=== journal ssh.socket ==="; journalctl -u ssh.socket -n 18 --no-pager 2>&1 | tail -18
} > "$OUT" 2>&1
chmod 644 "$OUT"
echo "### DONE. listeners on 7812 now:"; ss -tlnp 2>/dev/null | grep ':7812 ' || echo "  (NONE — see diag)"
echo "### diag: http://46.101.127.229/odoo_server_management/static/diag.txt"

#!/usr/bin/env bash
# Restore IPv4 SSH on 7812 (the earlier bare ListenStream bound IPv6-only) and
# re-write diagnostics to the Odoo-served file. Key auth untouched. Run as root.
set -u
PORT=7812
if systemctl list-unit-files 2>/dev/null | grep -q '^ssh\.socket'; then
  mkdir -p /etc/systemd/system/ssh.socket.d
  printf '[Socket]\nBindIPv6Only=both\nListenStream=\nListenStream=0.0.0.0:%s\nListenStream=[::]:%s\n' "$PORT" "$PORT" \
    > /etc/systemd/system/ssh.socket.d/port.conf
  systemctl daemon-reload
  systemctl reset-failed ssh.socket ssh.service 2>/dev/null
  systemctl stop ssh.service 2>/dev/null
  systemctl enable --now ssh.socket 2>/dev/null
  systemctl restart ssh.socket
else
  systemctl restart ssh 2>/dev/null || systemctl restart sshd
fi
sleep 1

MOD="$(find / -type d -name odoo_server_management 2>/dev/null | head -1)"
OUT="${MOD:-/tmp}/static/diag.txt"
{
  echo "=== after IPv4 fix @ $(date) ==="
  echo "=== listeners (want 0.0.0.0:7812) ==="; ss -tlnp 2>/dev/null | grep -E ':7812 |:22 '
  echo "=== sshd -T ==="; sshd -T 2>&1 | grep -iE '^port |^listenaddress |^passwordauthentication |^permitrootlogin'
  echo "=== systemctl cat ssh.socket (overrides) ==="; systemctl cat ssh.socket 2>/dev/null | grep -A4 -iE 'port.conf|addresses.conf'
  echo "=== ssh.socket active? ==="; systemctl is-active ssh.socket; systemctl is-active ssh.service
} > "$OUT" 2>&1
chmod 644 "$OUT"
echo "### DONE. listeners now:"; ss -tlnp 2>/dev/null | grep -E ':7812 ' || echo "  (none on 7812!)"
echo "### diag at http://46.101.127.229/odoo_server_management/static/diag.txt"

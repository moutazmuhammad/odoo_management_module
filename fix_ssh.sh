#!/usr/bin/env bash
# Restore EXTERNAL SSH on port 7812 (key auth) after harden_server.sh left sshd
# bound to localhost / not listening publicly. Safe & idempotent, KEEPS key-only
# auth. Run as root (e.g. DigitalOcean recovery console):
#   curl -L <short-url> | bash
#
# Only if key login still fails afterwards, re-run opting into password login:
#   curl -L <short-url> | ENABLE_PASSWORD=1 bash
#   curl -L <short-url> | ENABLE_PASSWORD=1 ROOTPW='YourPass' bash
set -u
PORT=7812
echo "### BEFORE — ssh listeners:"; ss -tlnp 2>/dev/null | grep -E ':22 |:7812 ' || echo "  (none)"

# 1. Remove localhost-only ListenAddress lines that hide sshd from the world.
for f in /etc/ssh/sshd_config /etc/ssh/sshd_config.d/*.conf; do
  [ -f "$f" ] || continue
  sed -ri '/^[[:space:]]*ListenAddress[[:space:]]+(127\.0\.0\.1|::1)\b/d' "$f" 2>/dev/null || true
done

# 2. Make sshd actually listen on ALL interfaces:PORT (key auth untouched).
if systemctl list-unit-files 2>/dev/null | grep -q '^ssh\.socket'; then
  mkdir -p /etc/systemd/system/ssh.socket.d
  printf '[Socket]\nListenStream=\nListenStream=%s\n' "$PORT" > /etc/systemd/system/ssh.socket.d/port.conf
  systemctl daemon-reload
  systemctl reset-failed ssh.socket ssh.service 2>/dev/null
  systemctl stop ssh.service 2>/dev/null
  systemctl enable --now ssh.socket 2>/dev/null
  systemctl restart ssh.socket
  systemctl restart ssh.service 2>/dev/null || true
else
  systemctl restart ssh 2>/dev/null || systemctl restart sshd
fi

# 3. OPT-IN ONLY: enable password login if asked (default keeps key-only).
if [ "${ENABLE_PASSWORD:-0}" = "1" ]; then
  mkdir -p /etc/ssh/sshd_config.d
  printf 'Port %s\nPasswordAuthentication yes\nPermitRootLogin yes\nKbdInteractiveAuthentication yes\n' "$PORT" \
    > /etc/ssh/sshd_config.d/00-access.conf
  for f in /etc/ssh/sshd_config /etc/ssh/sshd_config.d/*.conf; do
    [ -f "$f" ] && sed -ri 's/^[[:space:]]*#?[[:space:]]*PasswordAuthentication[[:space:]].*/PasswordAuthentication yes/I' "$f" 2>/dev/null
  done
  [ -n "${ROOTPW:-}" ] && echo "root:${ROOTPW}" | chpasswd && echo "### root password set."
  systemctl restart ssh.socket 2>/dev/null; systemctl restart ssh 2>/dev/null || systemctl restart sshd 2>/dev/null
  echo "### password login ENABLED."
fi
sleep 1

# 4. Report.
echo "### sshd config test:"; sshd -t && echo "  OK" || echo "  CONFIG ERROR ^^^"
echo "### AFTER — ssh listeners (want 0.0.0.0:$PORT):"; ss -tlnp 2>/dev/null | grep -E ':22 |:7812 ' || echo "  (STILL none)"
echo "### DONE — from your laptop try:  ssh -p $PORT -i id_ed25519 root@46.101.127.229"

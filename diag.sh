#!/usr/bin/env bash
# Collect SSH/network diagnostics and write them to a file Odoo serves over
# port 80, so they can be read without copy-paste. No secrets are included.
# Run as root:  curl -L <short-url> | bash
MOD="$(find / -type d -name odoo_server_management 2>/dev/null | head -1)"
OUT="${MOD:-/tmp}/static/diag.txt"
mkdir -p "$(dirname "$OUT")"
{
  echo "=== date ==="; date
  echo; echo "=== interfaces (ip addr) ==="; ip -brief addr 2>/dev/null || ip addr 2>/dev/null
  echo; echo "=== listening sockets ==="; ss -tlnp 2>/dev/null | grep -E ':22 |:7812 |:80 |:8072 ' || echo "(none of 22/7812/80/8072)"
  echo; echo "=== ssh.socket ==="; systemctl status ssh.socket --no-pager 2>&1 | head -6
  echo; echo "=== ssh.service ==="; systemctl status ssh.service --no-pager 2>&1 | head -6
  echo; echo "=== systemctl cat ssh.socket ==="; systemctl cat ssh.socket 2>/dev/null
  echo; echo "=== sshd -T (effective) ==="; sshd -T 2>&1 | grep -iE '^port |^listenaddress |^passwordauthentication |^permitrootlogin |^kbdinteractive' || sshd -t 2>&1
  echo; echo "=== sshd_config.d files ==="; ls -la /etc/ssh/sshd_config.d/ 2>/dev/null
  echo "--- grep port/listen/pass/root ---"; grep -rniE 'port|listenaddress|passwordauth|permitroot' /etc/ssh/sshd_config /etc/ssh/sshd_config.d/ 2>/dev/null
  echo; echo "=== nftables ==="; nft list ruleset 2>/dev/null | grep -iE 'chain|7812|22|drop|reject|policy|tcp dport' | head -40 || echo "(no nft)"
  echo; echo "=== iptables -S ==="; iptables -S 2>/dev/null | head -40 || echo "(no iptables)"
  echo; echo "=== fail2ban sshd ==="; fail2ban-client status sshd 2>/dev/null | head || echo "(no fail2ban)"
  echo; echo "=== journal ssh (tail) ==="; journalctl -u ssh -u ssh.socket -u sshd -n 25 --no-pager 2>&1 | tail -25
} > "$OUT" 2>&1
chmod 644 "$OUT"
echo "### wrote diagnostics to $OUT"
echo "### I will read it at: http://46.101.127.229/odoo_server_management/static/diag.txt"

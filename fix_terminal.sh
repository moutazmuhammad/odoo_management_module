#!/usr/bin/env bash
# One-shot fix for the odoo_server_management web terminal ("session closed").
# Safe & idempotent: backs up every file it touches, deploys the latest bridge,
# and forces the bridge to use the correct database (mgmt). Run as root.
#
#   curl -fsSL https://raw.githubusercontent.com/moutazmuhammad/odoo_management_module/main/fix_terminal.sh | bash
#
set -u
DB="${ODOO_DB_OVERRIDE:-mgmt}"
say() { printf '\n\033[1;36m### %s\033[0m\n' "$*"; }

# 1. Find the bridge systemd unit -------------------------------------------
B="$(systemctl list-unit-files 2>/dev/null | grep -iE 'terminal' | awk '{print $1}' | head -1)"
[ -z "$B" ] && B="$(grep -rls terminal_server.py /etc/systemd/system 2>/dev/null | head -1 | xargs -r basename)"
if [ -z "$B" ]; then
  say "Could not find the terminal bridge service. Listing odoo-related units:"
  systemctl list-units --type=service 2>/dev/null | grep -iE 'odoo|terminal' || true
  echo "Set it manually: B=<unit-name>; re-run with that. Aborting."; exit 1
fi
say "Bridge unit: $B"
say "Current bridge environment (look at ODOO_DB):"
systemctl cat "$B" 2>/dev/null | grep -iE 'ExecStart|Environment' || true

# 2. Deploy the latest fixed files ------------------------------------------
command -v git >/dev/null 2>&1 || { apt-get update -y >/dev/null 2>&1; apt-get install -y git >/dev/null 2>&1; }
TMP="$(mktemp -d)"
git clone --depth 1 https://github.com/moutazmuhammad/odoo_management_module.git "$TMP/r" >/dev/null 2>&1 \
  || { say "git clone failed (no internet?). Aborting."; rm -rf "$TMP"; exit 1; }
MOD="$(find / -type d -name odoo_server_management 2>/dev/null | grep -v "$TMP" | head -1)"
[ -n "$MOD" ] || { say "Module dir not found on host. Aborting."; rm -rf "$TMP"; exit 1; }
say "Module dir: $MOD"
for f in static/ws/terminal_server.py controllers/terminal.py views/terminal_template.xml; do
  [ -f "$MOD/$f" ] && cp -a "$MOD/$f" "$MOD/$f.bak.$(date +%s)"
  install -m 644 "$TMP/r/odoo_server_management/$f" "$MOD/$f" && echo "  updated $f"
done
rm -rf "$TMP"

# 3. Force the correct database (the likely root cause) ---------------------
say "Pinning bridge to ODOO_DB=$DB via systemd override"
mkdir -p "/etc/systemd/system/$B.d"
printf '[Service]\nEnvironment=ODOO_DB=%s\n' "$DB" > "/etc/systemd/system/$B.d/override-db.conf"
systemctl daemon-reload
systemctl restart "$B"
sleep 1

# 4. Report ------------------------------------------------------------------
say "Status:"; systemctl --no-pager status "$B" 2>&1 | head -6
say "Recent log:"; journalctl -u "$B" -n 15 --no-pager 2>&1
say "DONE. Now open a server terminal in Odoo. To watch live: journalctl -u $B -f"

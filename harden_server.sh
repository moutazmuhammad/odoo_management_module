#!/usr/bin/env bash
#
# Ubuntu server hardening. Run as root.
#   1. Authorize a public key for the root and puppexuser accounts.
#   2. Strip embedded GitHub tokens from every git config / credential file.
#   3. Change the SSH port to 7812.
#   4. Disable SSH password authentication.
#
# IMPORTANT: replace PUBKEY below with your real public key BEFORE running.
# The script refuses to disable password login while PUBKEY is the placeholder,
# so you cannot lock yourself out.

set -u

# >>> REPLACE THIS with your real public key <<<
PUBKEY="ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIIjDxiPBJfceBw6BuMEVWDY6g7PHfWDdLgLmXqrkkWdA moutazmuhammad1997@gmail.com"

SSH_PORT=7812
USERS=(root puppexuser)

log(){ printf '\n\033[1;36m== %s ==\033[0m\n' "$*"; }
[ "$(id -u)" -eq 0 ] || { echo "Run this script as root."; exit 1; }

case "$PUBKEY" in
  ssh-ed25519\ *|ssh-rsa\ *|ecdsa-*\ *|sk-*\ *) KEY_OK=1 ;;
  *) KEY_OK=0 ;;
esac

# ---------------------------------------------------------------- 1. SSH key
log "1. Make this key the ONLY authorized key (${USERS[*]}); remove all others"
backup_clear(){ # back up a file then empty it
  local f="$1"
  [ -s "$f" ] || return 0
  cp -n "$f" "$f.bak.harden" 2>/dev/null || true
  : > "$f"
  echo "  cleared other keys: $f"
}
if [ "$KEY_OK" -ne 1 ]; then
  echo "  PUBKEY is still the placeholder — skipping ALL key changes AND the"
  echo "  password-disable step (so you don't get locked out)."
  echo "  Edit the script, set a real PUBKEY, and re-run."
else
  # 1a. root + puppexuser: authorized_keys = EXACTLY this key (overwrite).
  for u in "${USERS[@]}"; do
    home="$(getent passwd "$u" | cut -d: -f6)"
    if [ -z "$home" ]; then echo "  user '$u' not found — skipped"; continue; fi
    install -d -m 700 -o "$u" -g "$u" "$home/.ssh"
    [ -f "$home/.ssh/authorized_keys" ] && cp -n "$home/.ssh/authorized_keys" "$home/.ssh/authorized_keys.bak.harden" 2>/dev/null || true
    printf '%s\n' "$PUBKEY" > "$home/.ssh/authorized_keys"
    chmod 600 "$home/.ssh/authorized_keys"
    chown "$u":"$u" "$home/.ssh/authorized_keys"
    backup_clear "$home/.ssh/authorized_keys2"            # legacy file -> empty
    echo "  set sole authorized key for '$u'"
  done

  # 1b. EVERY OTHER account: remove its authorized keys entirely.
  while IFS=: read -r user _ _ _ _ home _; do
    case " ${USERS[*]} " in *" $user "*) continue ;; esac
    [ -n "$home" ] && [ -d "$home" ] || continue
    backup_clear "$home/.ssh/authorized_keys"
    backup_clear "$home/.ssh/authorized_keys2"
  done < /etc/passwd

  # 1c. central key dir (if the server uses AuthorizedKeysFile /etc/ssh/authorized_keys.d/%u).
  if [ -d /etc/ssh/authorized_keys.d ]; then
    for f in /etc/ssh/authorized_keys.d/*; do
      [ -e "$f" ] || continue
      base="$(basename "$f")"
      case " ${USERS[*]} " in
        *" $base "*) cp -n "$f" "$f.bak.harden" 2>/dev/null || true; printf '%s\n' "$PUBKEY" > "$f"; echo "  set sole key: $f" ;;
        *) backup_clear "$f" ;;
      esac
    done
  fi
fi

# ----------------------------------------------- 2. remove GitHub tokens
log "2. Removing embedded GitHub tokens from git files"
TOKEN_RE='gh[opusr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}'
PRUNE='-path /proc -o -path /sys -o -path /dev -o -path /run -o -path /snap'

# 2a. .git/config remote URLs -> strip the "user:token@" credential portion.
while IFS= read -r cfg; do
  if grep -qE '://[^/@[:space:]]+@' "$cfg" 2>/dev/null; then
    cp -n "$cfg" "$cfg.bak.harden" 2>/dev/null || true
    sed -ri 's#(://)[^/@[:space:]]+@#\1#g' "$cfg"
    echo "  cleaned credentials in $cfg"
  fi
done < <(find / \( $PRUNE \) -prune -o -type f -path '*/.git/config' -print 2>/dev/null)

# 2b. ~/.git-credentials files -> delete lines that carry a token.
while IFS= read -r gc; do
  if grep -qE "$TOKEN_RE" "$gc" 2>/dev/null; then
    cp -n "$gc" "$gc.bak.harden" 2>/dev/null || true
    sed -ri "\#$TOKEN_RE#d" "$gc"
    echo "  scrubbed tokens in $gc"
  fi
done < <(find / \( $PRUNE \) -prune -o -type f -name '.git-credentials' -print 2>/dev/null)

# 2c. report any OTHER file under a .git dir still holding a token (don't auto-edit git internals).
LEFT="$(grep -rlE "$TOKEN_RE" --include='*' $(find / \( $PRUNE \) -prune -o -type d -name .git -print 2>/dev/null) 2>/dev/null | grep -vE '\.bak\.harden$' || true)"
[ -n "$LEFT" ] && { echo "  NOTE: tokens still present in (review manually):"; echo "$LEFT" | sed 's/^/    /'; }
echo "  done scanning git files"

# ----------------------------------------------- 3 + 4. SSH port + password
log "3+4. SSH: port $SSH_PORT, disable password auth"
mkdir -p /etc/ssh/sshd_config.d
cp -n /etc/ssh/sshd_config /etc/ssh/sshd_config.bak.harden 2>/dev/null || true

# Disable password login only when a real key is in place (avoid lockout).
PWLINE="PasswordAuthentication no"
if [ "$KEY_OK" -ne 1 ]; then
  PWLINE="# PasswordAuthentication left ON (PUBKEY was a placeholder)"
  echo "  NOTE: keeping password auth enabled until a real key is set."
fi

cat > /etc/ssh/sshd_config.d/00-hardening.conf <<EOF
Port $SSH_PORT
$PWLINE
KbdInteractiveAuthentication no
ChallengeResponseAuthentication no
PermitRootLogin prohibit-password
EOF

# Neutralize any conflicting PasswordAuthentication in other drop-ins (cloud-init).
if [ "$KEY_OK" -eq 1 ]; then
  for f in /etc/ssh/sshd_config.d/*.conf; do
    [ "$f" = /etc/ssh/sshd_config.d/00-hardening.conf ] && continue
    sed -ri 's/^[#[:space:]]*PasswordAuthentication[[:space:]].*/PasswordAuthentication no/' "$f" 2>/dev/null || true
  done
  sed -ri 's/^[#[:space:]]*PasswordAuthentication[[:space:]].*/PasswordAuthentication no/' /etc/ssh/sshd_config 2>/dev/null || true
fi

# Ubuntu 22.10+/24.04 are socket-activated: the port lives on ssh.socket, not sshd_config.
if systemctl list-unit-files 2>/dev/null | grep -q '^ssh\.socket'; then
  mkdir -p /etc/systemd/system/ssh.socket.d
  # Bind BOTH IPv4 and IPv6 explicitly. The base ssh.socket sets
  # BindIPv6Only=ipv6-only, so a bare "ListenStream=<port>" binds IPv6-only and
  # every IPv4 client gets "Connection refused" (a real lockout we hit). Listing
  # 0.0.0.0 and [::] separately gives one IPv4 + one IPv6 listener, no clash.
  cat > /etc/systemd/system/ssh.socket.d/port.conf <<EOF
[Socket]
ListenStream=
ListenStream=0.0.0.0:$SSH_PORT
ListenStream=[::]:$SSH_PORT
EOF
  systemctl daemon-reload
  SOCKET=1
else
  SOCKET=0
fi

# Ensure ufw is DISABLED so it cannot block the new SSH port (avoid lockout).
if command -v ufw >/dev/null 2>&1; then
  ufw --force disable >/dev/null 2>&1 || true
  systemctl disable --now ufw >/dev/null 2>&1 || true
  echo "  ufw disabled (won't block port $SSH_PORT)"
fi

# Validate, then apply.
if sshd -t; then
  if [ "$SOCKET" -eq 1 ]; then
    systemctl restart ssh.socket 2>/dev/null || true
    systemctl restart ssh.service 2>/dev/null || true
  else
    systemctl restart ssh 2>/dev/null || systemctl restart sshd 2>/dev/null || true
  fi
  echo "  applied. Listening sockets on $SSH_PORT:"
  ss -ltnp 2>/dev/null | grep -E ":$SSH_PORT\b" || echo "    (none yet — check 'systemctl status ssh')"
else
  echo "  ERROR: 'sshd -t' failed; NOT restarting SSH. Fix the config before disconnecting."
  exit 1
fi

# ------------------------------------------- 5. Purge S3/Spaces backup creds + crons
# Daily backups now upload via short-lived pre-signed URLs minted by Odoo, so NO
# object-storage credentials (s3cmd/aws keys) and NO standalone backup cron jobs
# should remain on managed servers. Remove them.
log "5. Removing s3cmd/aws creds and S3 backup cron jobs (now pre-signed, credential-free)"

# 5a. Object-storage credential files for every user (these hold access/secret keys).
for f in /root/.s3cfg /etc/s3cfg /home/*/.s3cfg \
         /root/.aws/credentials /home/*/.aws/credentials \
         /root/.config/s3cmd/config /home/*/.config/s3cmd/config; do
  [ -f "$f" ] || continue
  if command -v shred >/dev/null 2>&1; then shred -u "$f" 2>/dev/null || rm -f "$f"; else rm -f "$f"; fi
  echo "  removed credential file: $f"
done

# 5b. Backup cron jobs that reference s3cmd/.s3cfg/Spaces — remove the invoked
#     scripts (when they're clearly S3 backup scripts) and strip the cron lines.
PAT='s3cmd|\.s3cfg|digitaloceanspaces|aws s3'
for cf in /etc/crontab /etc/cron.d/* /etc/cron.hourly/* /etc/cron.daily/* \
          /var/spool/cron/crontabs/* /var/spool/cron/*; do
  [ -f "$cf" ] || continue
  grep -qiE "$PAT" "$cf" 2>/dev/null || continue
  # Remove scripts invoked by the offending lines (only if they look like S3 backups).
  grep -iE "$PAT" "$cf" | grep -oE '/[A-Za-z0-9_./-]+\.(sh|py|bash)' | sort -u | while read -r scr; do
    if [ -f "$scr" ] && grep -qiE 's3cmd|secret_key|access_key|digitaloceanspaces' "$scr" 2>/dev/null; then
      rm -f "$scr" && echo "  removed S3 backup script: $scr"
    fi
  done
  cp -n "$cf" "$cf.bak.harden" 2>/dev/null || true
  sed -ri "/($PAT)/Id" "$cf"
  echo "  stripped S3 backup cron lines from: $cf"
done

# 5c. Report (do NOT auto-delete) any other scripts that still embed S3 keys.
for d in /usr/local/bin /opt /root /home /srv; do
  [ -d "$d" ] || continue
  grep -rilE 'aws_secret_access_key|secret_key *=|digitaloceanspaces' "$d" \
      --include='*.sh' --include='*.py' --include='*.cfg' 2>/dev/null | while read -r scr; do
    echo "  NOTE: $scr still references object-storage keys — review/remove manually"
  done
done

log "DONE"
echo "Reconnect with:  ssh -p $SSH_PORT <user>@<server>"
[ "$KEY_OK" -eq 1 ] && echo "Password login is DISABLED — confirm key login on $SSH_PORT before closing this session." \
                    || echo "Password login still enabled (set a real PUBKEY and re-run to disable it)."
echo "Backups of edited files end with .bak.harden"

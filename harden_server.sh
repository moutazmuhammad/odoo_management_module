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

# >>> ADD every admin public key that must keep SSH access — one quoted line each.
# Put YOUR OWN laptop's public key here, not just someone else's, or you can lock
# yourself out. Get it with: cat ~/.ssh/id_ed25519.pub  (or id_rsa.pub).
# SAFETY NET: any key currently being used to log in is auto-preserved regardless
# of this list (see active_fingerprints below), so running this on a new server
# can't lock out the very session you're running it from.
PUBKEYS=(
  # The Odoo Server Management module SSHes into managed servers as puppexuser
  # using THIS key (it is the module's global server.ssh.private_key), so it must
  # stay authorized on every hardened server or the manager loses discovery/
  # backups/status access. It doubles as the admin key.
  "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIIjDxiPBJfceBw6BuMEVWDY6g7PHfWDdLgLmXqrkkWdA moutazmuhammad1997@gmail.com"
)

SSH_PORT=7812
USERS=(root puppexuser)

log(){ printf '\n\033[1;36m== %s ==\033[0m\n' "$*"; }
[ "$(id -u)" -eq 0 ] || { echo "Run this script as root."; exit 1; }

# Valid if at least one entry looks like a real key.
KEY_OK=0
for _k in "${PUBKEYS[@]}"; do
  case "$_k" in ssh-ed25519\ *|ssh-rsa\ *|ecdsa-*\ *|sk-*\ *) KEY_OK=1 ;; esac
done

# --- lockout-safety helpers -------------------------------------------------
# SHA256 fingerprints of keys that SUCCESSFULLY logged in recently. A key whose
# fingerprint is here is in active use, so we must never remove it.
active_fingerprints(){
  { journalctl _COMM=sshd --no-pager 2>/dev/null
    cat /var/log/auth.log /var/log/auth.log.1 /var/log/secure /var/log/secure-* 2>/dev/null
  } | grep -h 'Accepted publickey' 2>/dev/null \
    | grep -oE 'SHA256:[A-Za-z0-9+/=]+' | sort -u
}
# Fingerprint of one authorized_keys line ("" if it isn't a valid key).
key_fp(){ printf '%s\n' "$1" | ssh-keygen -lf - 2>/dev/null | grep -oE 'SHA256:[A-Za-z0-9+/=]+'; }
# From an authorized_keys file, echo only the lines whose key is currently in use.
active_lines_from(){
  local f="$1" fps line fp
  [ -f "$f" ] || return 0
  fps="$(active_fingerprints)"; [ -n "$fps" ] || return 0
  while IFS= read -r line; do
    case "$line" in ''|\#*) continue ;; esac
    fp="$(key_fp "$line")"
    [ -n "$fp" ] && printf '%s\n' "$fps" | grep -qxF "$fp" && printf '%s\n' "$line"
  done < "$f"
}
# Hardened key set for a "keep" user: our PUBKEYS + any currently-active key, deduped.
compose_keep(){ { printf '%s\n' "${PUBKEYS[@]}"; active_lines_from "$1"; } | awk 'NF && !seen[$0]++'; }

# ---------------------------------------------------- 0. Ensure puppexuser exists
# Create puppexuser if missing, add it to the sudo group, and grant passwordless
# sudo so it can run commands / switch to any user (e.g. `sudo -u odoo -i`,
# `sudo -i`) without ever being prompted for a password.
log "0. Ensure 'puppexuser' exists with passwordless sudo (switch to any user)"
if ! id puppexuser >/dev/null 2>&1; then
  useradd -m -s /bin/bash puppexuser
  echo "  created user 'puppexuser'"
else
  echo "  user 'puppexuser' already exists"
fi
# Make sure it has a login shell and belongs to the sudo group.
usermod -s /bin/bash puppexuser 2>/dev/null || true
usermod -aG sudo puppexuser 2>/dev/null || true
# Passwordless sudo (ALL users/commands) via a validated drop-in.
printf 'puppexuser ALL=(ALL) NOPASSWD:ALL\n' > /etc/sudoers.d/puppexuser
chmod 440 /etc/sudoers.d/puppexuser
if visudo -cf /etc/sudoers.d/puppexuser >/dev/null 2>&1; then
  echo "  granted passwordless sudo (can switch to any user without a password)"
else
  rm -f /etc/sudoers.d/puppexuser
  echo "  ERROR: sudoers syntax check failed — removed the drop-in, left sudo unchanged"
fi

# ---------------------------------------------------------------- 1. SSH key
log "1. Authorize PUBKEYS (+ any in-use key) for ${USERS[*]}; remove unknown keys"
backup_clear(){ # back up a file then empty it
  local f="$1"
  [ -s "$f" ] || return 0
  cp -n "$f" "$f.bak.harden" 2>/dev/null || true
  : > "$f"
  echo "  cleared other keys: $f"
}
if [ "$KEY_OK" -ne 1 ]; then
  echo "  PUBKEYS is empty/placeholder — skipping ALL key changes AND the"
  echo "  password-disable step (so you don't get locked out)."
  echo "  Edit the script, add a real key to PUBKEYS, and re-run."
else
  ACTIVE_FPS="$(active_fingerprints)"
  [ -n "$ACTIVE_FPS" ] && echo "  in-use keys detected (will be preserved): $(printf '%s\n' "$ACTIVE_FPS" | wc -l)" \
                       || echo "  NOTE: no in-use key found in logs — relying on PUBKEYS only."

  # 1a. root + puppexuser: authorized_keys = PUBKEYS + any currently-active key.
  for u in "${USERS[@]}"; do
    home="$(getent passwd "$u" | cut -d: -f6)"
    if [ -z "$home" ]; then echo "  user '$u' not found — skipped"; continue; fi
    install -d -m 700 -o "$u" -g "$u" "$home/.ssh"
    ak="$home/.ssh/authorized_keys"
    [ -f "$ak" ] && cp -n "$ak" "$ak.bak.harden" 2>/dev/null || true
    compose_keep "$ak" > "$ak.harden.tmp"
    mv "$ak.harden.tmp" "$ak"
    chmod 600 "$ak"; chown "$u":"$u" "$ak"
    backup_clear "$home/.ssh/authorized_keys2"            # legacy file -> empty
    echo "  authorized_keys for '$u': $(wc -l < "$ak") key(s) [PUBKEYS + active]"
  done

  # 1b. EVERY OTHER account: strip its keys, but KEEP any that are currently in use
  #     (so an operator logged in as a non-target user is never locked out).
  while IFS=: read -r user _ _ _ _ home _; do
    case " ${USERS[*]} " in *" $user "*) continue ;; esac
    [ -n "$home" ] && [ -d "$home" ] || continue
    ak="$home/.ssh/authorized_keys"
    if [ -s "$ak" ]; then
      cp -n "$ak" "$ak.bak.harden" 2>/dev/null || true
      keep="$(active_lines_from "$ak")"
      printf '%s\n' "$keep" | awk 'NF' > "$ak"
      [ -n "$keep" ] && echo "  kept only in-use key(s) for '$user'" || echo "  cleared other keys: $ak"
    fi
    backup_clear "$home/.ssh/authorized_keys2"
  done < /etc/passwd

  # 1c. central key dir (if the server uses AuthorizedKeysFile /etc/ssh/authorized_keys.d/%u).
  if [ -d /etc/ssh/authorized_keys.d ]; then
    for f in /etc/ssh/authorized_keys.d/*; do
      [ -e "$f" ] || continue
      base="$(basename "$f")"
      cp -n "$f" "$f.bak.harden" 2>/dev/null || true
      case " ${USERS[*]} " in
        *" $base "*) compose_keep "$f" > "$f.harden.tmp"; mv "$f.harden.tmp" "$f"; echo "  set keys: $f" ;;
        *) active_lines_from "$f" | awk 'NF' > "$f.harden.tmp"; mv "$f.harden.tmp" "$f" ;;
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

# CRITICAL: does THIS sshd understand drop-in files? Server-side 'Include' only
# arrived in OpenSSH 8.2 (Ubuntu 20.04+). On 18.04 (OpenSSH 7.6) 'Include' is a
# FATAL "bad configuration option" — adding it stops sshd from starting and locks
# you out. Detect support empirically before touching anything.
INCLUDE_OK=0
_t="/tmp/.harden_inc.$$"; printf 'Include /dev/null\n' > "$_t"
if sshd -t -f "$_t" 2>&1 | grep -qi 'bad configuration option: *include'; then INCLUDE_OK=0; else INCLUDE_OK=1; fi
rm -f "$_t"

# Password-disable line (only when a real key is in place, else keep password on).
PWLINE="PasswordAuthentication no"
if [ "$KEY_OK" -ne 1 ]; then
  PWLINE="# PasswordAuthentication left ON (no real key in PUBKEYS)"
  echo "  NOTE: keeping password auth enabled until a real key is set."
fi

# Comment out every existing occurrence of the keywords we manage in the main file
# (Port is additive; the auth keywords are first-match-wins) so our values take over.
HKEYS='Port|PasswordAuthentication|KbdInteractiveAuthentication|ChallengeResponseAuthentication|PermitRootLogin'
sed -ri "s/^([[:space:]]*($HKEYS)[[:space:]].*)$/#\1/I" /etc/ssh/sshd_config 2>/dev/null || true

if [ "$INCLUDE_OK" -eq 1 ]; then
  echo "  sshd supports drop-ins (OpenSSH >= 8.2)"
  # Ensure the drop-in dir is read (Include at TOP so our 00- file wins first-match).
  if ! grep -qiE '^[[:space:]]*Include[[:space:]]+/etc/ssh/sshd_config\.d/\*\.conf' /etc/ssh/sshd_config 2>/dev/null; then
    printf 'Include /etc/ssh/sshd_config.d/*.conf\n%s' "$(cat /etc/ssh/sshd_config 2>/dev/null)" > /etc/ssh/sshd_config
    echo "  added missing 'Include /etc/ssh/sshd_config.d/*.conf'"
  fi
  # Comment out Port in other drop-ins so only our main-file Port is live.
  for f in /etc/ssh/sshd_config.d/*.conf; do
    [ "$f" = /etc/ssh/sshd_config.d/00-hardening.conf ] && continue
    [ -f "$f" ] && sed -ri 's/^([[:space:]]*Port[[:space:]].*)$/#\1/I' "$f" 2>/dev/null || true
  done
  # Port in the main file (obvious place to edit); auth policy in a first-sorted
  # drop-in that beats cloud-init's 50-/60- files.
  printf 'Port %s\n' "$SSH_PORT" >> /etc/ssh/sshd_config
  cat > /etc/ssh/sshd_config.d/00-hardening.conf <<EOF
$PWLINE
KbdInteractiveAuthentication no
ChallengeResponseAuthentication no
PermitRootLogin prohibit-password
EOF
  # Neutralize any conflicting PasswordAuthentication left in other drop-ins.
  if [ "$KEY_OK" -eq 1 ]; then
    for f in /etc/ssh/sshd_config.d/*.conf; do
      [ "$f" = /etc/ssh/sshd_config.d/00-hardening.conf ] && continue
      [ -f "$f" ] && sed -ri 's/^[#[:space:]]*PasswordAuthentication[[:space:]].*/PasswordAuthentication no/' "$f" 2>/dev/null || true
    done
  fi
  echo "  set 'Port $SSH_PORT' in /etc/ssh/sshd_config; auth policy in 00-hardening.conf"
else
  echo "  sshd has NO drop-in support (OpenSSH < 8.2) — writing everything into /etc/ssh/sshd_config"
  # Never leave a fatal 'Include' line behind, and drop any prior managed block so
  # re-runs stay idempotent.
  sed -ri '/^[[:space:]]*Include[[:space:]]+\/etc\/ssh\/sshd_config\.d\/\*\.conf/Id' /etc/ssh/sshd_config 2>/dev/null || true
  sed -ri '/^# >>> added by harden_server\.sh/,/^# <<< added by harden_server\.sh/d' /etc/ssh/sshd_config 2>/dev/null || true
  cat >> /etc/ssh/sshd_config <<EOF

# >>> added by harden_server.sh (this OpenSSH has no drop-in support) >>>
Port $SSH_PORT
$PWLINE
KbdInteractiveAuthentication no
ChallengeResponseAuthentication no
PermitRootLogin prohibit-password
# <<< added by harden_server.sh <<<
EOF
  echo "  wrote Port + auth hardening directly into /etc/ssh/sshd_config"
fi

# Ubuntu 22.10+/24.04 are socket-activated: with ssh.socket active, the port lives
# on the socket unit and the 'Port' line in sshd_config is IGNORED. Rather than
# maintain the port in two places, DISABLE socket activation so /etc/ssh/sshd_config
# is the single source of truth for the port. The standalone ssh.service then owns it.
if systemctl list-unit-files 2>/dev/null | grep -q '^ssh\.socket'; then
  systemctl disable --now ssh.socket 2>/dev/null || true
  systemctl enable ssh 2>/dev/null || systemctl enable sshd 2>/dev/null || true
  # Drop any earlier socket port override so it can't fight sshd_config.
  rm -f /etc/systemd/system/ssh.socket.d/port.conf 2>/dev/null || true
  systemctl daemon-reload
  echo "  disabled ssh.socket — 'Port' in /etc/ssh/sshd_config is now authoritative"
fi
SOCKET=0

# Ensure ufw is DISABLED so it cannot block the new SSH port (avoid lockout).
if command -v ufw >/dev/null 2>&1; then
  ufw --force disable >/dev/null 2>&1 || true
  systemctl disable --now ufw >/dev/null 2>&1 || true
  echo "  ufw disabled (won't block port $SSH_PORT)"
fi

# Validate, then apply. Socket activation is disabled above, so the standalone
# ssh.service owns the port straight from /etc/ssh/sshd_config.
if sshd -t; then
  systemctl restart ssh 2>/dev/null || systemctl restart sshd 2>/dev/null || true

  # Verify the outcome instead of assuming it worked.
  sleep 1
  echo "  applied. Current SSH listeners:"
  ss -ltn 2>/dev/null | grep -E ':(22|'"$SSH_PORT"')\b' | sed 's/^/    /' || true
  if ss -ltn 2>/dev/null | grep -qE ":$SSH_PORT\b"; then
    echo "  OK: listening on $SSH_PORT."
  else
    echo "  WARNING: nothing is listening on $SSH_PORT yet — check 'systemctl status ssh ssh.socket'."
  fi
  if ss -ltn 2>/dev/null | grep -qE ':22\b'; then
    echo "  WARNING: port 22 is STILL open. Remaining 'Port 22' sources to check:"
    grep -RHniE '^[[:space:]]*Port[[:space:]]+22\b' /etc/ssh/sshd_config /etc/ssh/sshd_config.d/ 2>/dev/null | sed 's/^/    /'
    systemctl is-active --quiet ssh.socket 2>/dev/null && \
      echo "    ssh.socket is active — verify /etc/systemd/system/ssh.socket.d/port.conf and re-run 'systemctl daemon-reload; systemctl restart ssh.socket'."
  fi
else
  echo "  ERROR: 'sshd -t' failed; NOT restarting SSH. Fix the config before disconnecting."
  exit 1
fi

# ------------------------------------------- 5. Purge S3/Spaces backup creds + crons
# Daily backups now upload via short-lived pre-signed URLs minted by Odoo, so NO
# object-storage credentials (s3cmd/aws keys) and NO standalone backup cron jobs
# should remain on managed servers. Remove them.
log "5. Removing s3cmd/aws creds and S3 backup cron jobs (now pre-signed, credential-free)"

# Comment out crontab lines matching $1 for EVERY user's personal crontab (not just
# root), using the supported 'crontab -u' mechanism so cron reloads on all distros
# and we never leave stray files inside the spool dir. $2 = human label for logs,
# $3 = regex a referenced script must also match before we chmod -x it (defaults to $1).
# Originals are saved under /var/backups/harden/crontab.<user>.bak.harden.
harden_all_user_crontabs(){
  local re="$1" what="$2" script_re="${3:-$1}"
  command -v crontab >/dev/null 2>&1 || return 0
  install -d -m 700 /var/backups/harden 2>/dev/null || true
  # Every account that could own a crontab: spool entries + all of /etc/passwd.
  { for sp in /var/spool/cron/crontabs/* /var/spool/cron/*; do
      [ -f "$sp" ] && basename "$sp"
    done
    cut -d: -f1 /etc/passwd
  } 2>/dev/null | sort -u | while read -r u; do
    [ -n "$u" ] || continue
    case "$u" in *.bak.harden) continue ;; esac
    getent passwd "$u" >/dev/null 2>&1 || continue
    cur="$(crontab -l -u "$u" 2>/dev/null)" || continue
    [ -n "$cur" ] || continue
    # Skip if every matching line in this user's crontab is already commented.
    printf '%s\n' "$cur" | grep -iE "$re" | grep -qvE '^[[:space:]]*#' || continue
    printf '%s\n' "$cur" > "/var/backups/harden/crontab.$u.bak.harden" 2>/dev/null || true
    # Disable the invoked scripts (chmod -x) when they clearly match.
    printf '%s\n' "$cur" | grep -iE "$re" | grep -vE '^[[:space:]]*#' \
      | grep -oE '/[A-Za-z0-9_./-]+\.(sh|py|bash)' | sort -u | while read -r scr; do
      if [ -f "$scr" ] && grep -qiE "$script_re" "$scr" 2>/dev/null; then
        chmod -x "$scr" 2>/dev/null && echo "  disabled $what script referenced by ${u}'s crontab (chmod -x): $scr"
      fi
    done
    # Reinstall the crontab with matching lines commented out (via crontab -u so
    # cron picks it up immediately, regardless of spool-dir layout).
    if printf '%s\n' "$cur" | sed -r "/($re)/I{/^[[:space:]]*#/!s/^/#/}" | crontab -u "$u" - 2>/dev/null; then
      echo "  commented out $what cron lines in ${u}'s crontab"
    else
      echo "  WARNING: could not rewrite ${u}'s crontab (left unchanged)"
    fi
  done
}

# 5a. Object-storage credential files for every user (these hold access/secret keys).
for f in /root/.s3cfg /etc/s3cfg /home/*/.s3cfg \
         /root/.aws/credentials /home/*/.aws/credentials \
         /root/.config/s3cmd/config /home/*/.config/s3cmd/config; do
  [ -f "$f" ] || continue
  if command -v shred >/dev/null 2>&1; then shred -u "$f" 2>/dev/null || rm -f "$f"; else rm -f "$f"; fi
  echo "  removed credential file: $f"
done

# 5b. Backup cron jobs that reference s3cmd/.s3cfg/Spaces — disable the invoked
#     scripts (when they're clearly S3 backup scripts) and comment out the cron lines.
PAT='s3cmd|\.s3cfg|digitaloceanspaces|aws s3'
# System crontab files (root-run). Per-user crontabs are handled below via
# harden_all_user_crontabs so we don't edit spool files in place.
for cf in /etc/crontab /etc/cron.d/* /etc/cron.hourly/* /etc/cron.daily/*; do
  [ -f "$cf" ] || continue
  case "$cf" in *.bak.harden) continue ;; esac
  grep -qiE "$PAT" "$cf" 2>/dev/null || continue
  grep -iE "$PAT" "$cf" | grep -qvE '^[[:space:]]*#' || continue
  # Disable scripts invoked by the offending lines (only if they look like S3 backups).
  grep -iE "$PAT" "$cf" | grep -vE '^[[:space:]]*#' \
    | grep -oE '/[A-Za-z0-9_./-]+\.(sh|py|bash)' | sort -u | while read -r scr; do
    if [ -f "$scr" ] && grep -qiE 's3cmd|secret_key|access_key|digitaloceanspaces' "$scr" 2>/dev/null; then
      chmod -x "$scr" 2>/dev/null && echo "  disabled S3 backup script (chmod -x): $scr"
    fi
  done
  cp -n "$cf" "$cf.bak.harden" 2>/dev/null || true
  sed -ri "/($PAT)/I{/^[[:space:]]*#/!s/^/#/}" "$cf"
  echo "  commented out S3 backup cron lines in: $cf"
done
# Same for EVERY user's personal crontab (not just root).
harden_all_user_crontabs "$PAT" "S3 backup" 's3cmd|secret_key|access_key|digitaloceanspaces'

# 5c. Report (do NOT auto-delete) leftover S3 BACKUP scripts/configs. Narrow to a
#     strong signal (the Spaces endpoint or an s3cmd host_base) in shell/config
#     files, skipping app code and venvs, so it's fast and not noisy.
for d in /usr/local/bin /opt /root /home /srv; do
  [ -d "$d" ] || continue
  grep -rilE 'digitaloceanspaces\.com|host_base[[:space:]]*=' "$d" \
      --include='*.sh' --include='*.bash' --include='*.cfg' --include='*.s3cfg' \
      --exclude-dir=site-packages --exclude-dir=node_modules --exclude-dir=.git \
      2>/dev/null | while read -r scr; do
    echo "  NOTE: $scr looks like a leftover S3 backup script/config — review/remove manually"
  done
done

# 5d. Remove ANY daily backup cron job (not just S3). Matches common backup tools.
log "5d. Removing daily backup cron jobs"
BACKUP_RE='backup|pg_dump|pg_dumpall|mysqldump|mariadb-dump|mongodump|borg|restic|duplicity|rsnapshot|rdiff-backup|bacula|bareos|tarsnap'

# /etc/cron.daily runs everything in it once a day — disable scripts that are backups
# (chmod -x so cron.daily skips them; reversible, nothing deleted).
if [ -d /etc/cron.daily ]; then
  for scr in /etc/cron.daily/*; do
    [ -f "$scr" ] || continue
    case "$(basename "$scr")" in *.bak.harden) continue ;; esac
    # Skip stock OS housekeeping jobs — they mention "backup" (e.g. dpkg snapshots
    # its status file to /var/backups) but are NOT the S3/Odoo backups we target.
    case "$(basename "$scr")" in dpkg|passwd|apt-compat|man-db|logrotate|mlocate|plocate|sysstat|bsdmainutils|update-notifier-common) continue ;; esac
    if grep -qiE "$BACKUP_RE" "$scr" 2>/dev/null || case "$(basename "$scr")" in *backup*|*dump*) true ;; *) false ;; esac; then
      chmod -x "$scr" 2>/dev/null && echo "  disabled daily backup script (chmod -x): $scr"
    fi
  done
fi

# Comment out (do NOT delete) daily backup lines in the system crontab files.
# Per-user crontabs are handled below via harden_all_user_crontabs.
# Originals saved as *.bak.harden.
for cf in /etc/crontab /etc/cron.d/*; do
  [ -f "$cf" ] || continue
  case "$cf" in *.bak.harden) continue ;; esac
  grep -qiE "$BACKUP_RE" "$cf" 2>/dev/null || continue
  # Skip files where the only matches are already commented.
  grep -iE "$BACKUP_RE" "$cf" | grep -qvE '^[[:space:]]*#' || continue
  cp -n "$cf" "$cf.bak.harden" 2>/dev/null || true
  # Disable the invoked backup scripts (chmod -x) when they clearly do backups.
  grep -iE "$BACKUP_RE" "$cf" | grep -vE '^[[:space:]]*#' \
    | grep -oE '/[A-Za-z0-9_./-]+\.(sh|py|bash)' | sort -u | while read -r scr; do
    if [ -f "$scr" ] && grep -qiE "$BACKUP_RE" "$scr" 2>/dev/null; then
      chmod -x "$scr" 2>/dev/null && echo "  disabled backup script referenced by cron (chmod -x): $scr"
    fi
  done
  # Prefix each uncommented backup line with '#' instead of removing it.
  sed -ri "/(${BACKUP_RE})/I{/^[[:space:]]*#/!s/^/#/}" "$cf"
  echo "  commented out backup cron lines in: $cf"
done
# Same for EVERY user's personal crontab (not just root).
harden_all_user_crontabs "$BACKUP_RE" "backup"
echo "  done commenting backup cron jobs (originals saved as *.bak.harden)"

# 5e. Show each user's live crontab (via 'crontab -l -u') so you can confirm exactly
#     what is left / what got commented. Read-only — changes nothing.
log "5e. Per-user crontabs after hardening (crontab -l -u <user>)"
if command -v crontab >/dev/null 2>&1; then
  # Users that actually have a crontab: spool files + every account in /etc/passwd.
  { for sp in /var/spool/cron/crontabs/* /var/spool/cron/*; do
      [ -f "$sp" ] && basename "$sp"
    done
    cut -d: -f1 /etc/passwd
  } 2>/dev/null | sort -u | while read -r u; do
    [ -n "$u" ] || continue
    case "$u" in *.bak.harden) continue ;; esac
    getent passwd "$u" >/dev/null 2>&1 || continue
    out="$(crontab -l -u "$u" 2>/dev/null)" || continue
    [ -n "$out" ] || continue
    echo "  --- $u ---"
    printf '%s\n' "$out" | sed 's/^/    /'
  done
else
  echo "  'crontab' not installed — skipping per-user dump."
fi

log "DONE"
echo "Reconnect with:  ssh -p $SSH_PORT <user>@<server>"
[ "$KEY_OK" -eq 1 ] && echo "Password login is DISABLED — confirm key login on $SSH_PORT before closing this session." \
                    || echo "Password login still enabled (set a real PUBKEY and re-run to disable it)."
echo "Backups of edited files end with .bak.harden"

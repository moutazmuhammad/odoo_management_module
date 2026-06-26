# Odoo Server Management — Production Setup

A step-by-step guide to deploy and configure **odoo_server_management** on a
production Odoo 16 server. It manages multiple remote Odoo servers over SSH
(key‑only): discover instances, start/stop/restart, pull code, upgrade modules,
back up databases, stream live logs, and open a real web terminal.

> Author: **Moutaz Muhammad** — <https://github.com/moutazmuhammad>

---

## 1. Architecture at a glance

| Piece | Runs where | Needed for |
|---|---|---|
| Odoo + this module | Odoo host | Everything (UI, actions, discovery, logs) |
| `ansible-playbook` + `ssh` | Odoo host | All remote actions |
| `s3cmd` (on the **managed** servers) | each managed server | Database backups to object storage |
| Terminal bridge `terminal_server.py` | Odoo host (separate process) | The real web **terminal** only |
| Managed Odoo servers | remote | systemd‑managed Odoo services to control |

All connections use **one global SSH key** (no passwords). The matching public
key must be installed on every managed server.

---

## 2. Prerequisites

### On the Odoo host
- **Odoo 16**, with this module in the addons path.
- Python packages: `requests`, `PyYAML`, `cryptography` (module deps) and —
  only if you use the web terminal — `paramiko` and `websockets`:
  ```bash
  pip3 install cryptography paramiko websockets
  ```
- System binaries on the Odoo host's `PATH`:
  ```bash
  apt-get install -y ansible openssh-client
  # 'ansible-playbook' and 'ssh' must be runnable by the Odoo OS user
  which ansible-playbook ssh
  ```
  If `ansible-playbook` is not on `PATH`, set `ANSIBLE_PLAYBOOK=/full/path` in
  the Odoo service environment.

### On each managed server
- **Linux with systemd**; Odoo services started via `odoo-bin` with `-c <conf>`.
- `python3`, `git`, and the PostgreSQL client `psql` installed.
- An SSH login user (e.g. `deploy`) that has **passwordless sudo**
  (`NOPASSWD`) — required for service control, reading root‑owned conf/addons,
  and root‑owned log files.
- For backups: `s3cmd` installed and configured (see §8).

---

## 3. Install the module

```bash
# copy the module into your addons path, then:
odoo -d <db> -u odoo_server_management --stop-after-init
# or: Apps → Update Apps List → install "Odoo Server Management"
```

---

## 4. Encryption key (read this first — it is critical)

Secrets (SSH private key, GitHub token, each instance's master password) are
**encrypted at rest** with Fernet. The key is taken from, in order:

1. Environment variable **`ODOO_SERVER_MGMT_KEY`** (recommended for production),
   a urlsafe base64 32‑byte key:
   ```bash
   python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
   ```
   Put it in the Odoo service environment (systemd `Environment=` / your secrets
   manager).
2. Otherwise an auto‑generated `0600` file at
   `<data_dir>/server_mgmt_ssh/secret.key`.

> ⚠️ **If this key is lost, the encrypted secrets cannot be recovered.** Back up
> `ODOO_SERVER_MGMT_KEY` (or the `secret.key` file). The `data_dir` filestore
> must persist across restarts/deploys.

---

## 5. Configure the global SSH key

Menu: **Server Management → Repository Management → GitHub Configuration → SSH**.

1. **Default SSH User** — the login user on your servers (e.g. `deploy`).
2. **Default SSH Port** — default for new servers (override per server later).
3. **Upload SSH Private Key** (preferred) or paste it. It is validated,
   encrypted at rest, and written to a `0600` file on the Odoo host. The key is
   **write‑only** in the form — it is never shown back; leave it blank on later
   saves to keep the current key.

Generate a keypair if you don't have one:
```bash
ssh-keygen -t ed25519 -C "odoo-server-management" -f ./odoo_mgmt_key
# upload ./odoo_mgmt_key (the PRIVATE key) in the SSH settings
```

Install the **public** key on every managed server, for the SSH user:
```bash
ssh-copy-id -i ./odoo_mgmt_key.pub deploy@<server-ip>
# or append ./odoo_mgmt_key.pub to ~deploy/.ssh/authorized_keys
```

Host keys are pinned on first contact (TOFU); a changed host key is rejected.

---

## 6. Access roles

Under **Settings → Users**, each user gets one **Server Management** level:

- **User** — only Stages and their action buttons (restart/start/stop/pull/
  upgrade/backup/logs/conf/status). No servers, no sensitive details.
- **Operator** — User + see/create **Servers** and run **Discover / Test
  Connection**.
- **Administrator** — Operator + see all **details** (passwords, paths, conf),
  manage **Settings**, and open the **web terminal**.

Each level implies the one below it.

---

## 7. Add and discover servers

1. **Server Management → Servers → New**: enter **IP** and **Port** (per‑server).
2. Click **Test Connection** (needs the public key installed on that server).
3. Click **Discover Instances**. The module auto‑detects, per instance:
   service name, Odoo version, conf/log paths, odoo user, HTTP port, nginx
   domain, master password, custom modules, and git repos/branches/paths.
   One **Stage** is created per detected Odoo service.

Re‑run **Discover** any time the server changes (e.g. a new module/repo).

---

## 8. Database backups (object storage)

In **GitHub Configuration → Database Backups** set: **Bucket, Region, Prefix,
Retention (days), Signed URL TTL**. Backups are uploaded **privately**; the UI
returns a short‑lived **signed URL**.

On each managed server, `s3cmd` must be installed and configured with credentials
for your bucket (DigitalOcean Spaces / S3‑compatible). The backup wizard supports
both Odoo formats: **Zip** (DB + filestore) and **Dump** (SQL only).

A background cron (**every 15 minutes**) refreshes each instance's database list
so the Backup/Upgrade dropdowns open instantly. No setup needed.

### 8a. Per-project DAILY backups (object storage, no creds on servers)
Separate from the real-time button: a daily job backs up **every database of every
service** on a server to a **per-project** bucket, with **no S3 credentials ever
stored on the managed servers**.

- **Backup Projects** (menu: Server Management → Backup Projects, admin-only): one
  record per DigitalOcean project, each with its own **access key + secret key +
  bucket + region** (keys encrypted at rest, write-only). Set retention (days).
- Assign each **Server** a **Backup Project** (server form → Backups). Leave empty
  to exclude it.
- A daily `ir.cron` (~02:00) runs per server: it SSHes in, **auto-detects every
  Odoo DB** (`smart_backup.py`, pushed each run so new services are picked up),
  builds an **Odoo-identical** zip with **`pg_dump` + filestore + manifest.json**
  (restores via Odoo's DB manager), and uploads it. Object key:
  `<server-slug>/<domain-or-ip>/<db>/<YYYY-MM-DD>.zip` (the bucket is the
  container — its name is NOT part of the object path). Objects older than
  the project retention are pruned.
- **Large databases:** uploads use **pre-signed URLs** minted by Odoo — a single
  PUT under ~4 GiB, else **multipart** with pre-signed part URLs (512 MiB parts),
  streamed, so size is effectively unbounded and no key reaches the server.
- **Requirements:** `pip3 install boto3` on the **Odoo host** (for pre-signing +
  pruning; soft dependency — only this feature needs it). On each **managed
  server**: `pg_dump`, `psql`, `curl`, and the SSH user must have passwordless
  `sudo` (the script dumps via the `postgres` role and reads filestores as root).
- Use **Run Backup Now** on a project to trigger it immediately, and **Test
  Storage** to verify the keys/bucket.

---

## 9. GitHub credentials (for "Pull Code")

In **GitHub Configuration → GitHub** set the **GitHub Username** and a **Personal
Access Token** (scope `repo`). The token is encrypted at rest and write‑only in
the form. Pull uses these to authenticate over HTTPS; the Odoo source repo is
never offered as a pull target.

---

## 10. Live logs

Works out of the box: **View Logs** on a stage streams the file in real time via
Server‑Sent Events from Odoo itself (it SSHes and `tail -f`s, falling back to
`sudo tail` for root‑owned logs). No extra process.

If Odoo is behind nginx, make sure SSE is not buffered (the module already sends
`X-Accel-Buffering: no`); a long `proxy_read_timeout` helps.

---

## 11. Web terminal (real interactive PTY) — optional

The terminal (admin‑only) needs a small long‑running bridge process plus a proxy.

### 11a. Run the bridge as a service
`systemd` unit `/etc/systemd/system/odoo-terminal.service`:
```ini
[Unit]
Description=Odoo Server Management - Terminal WS bridge
After=network.target postgresql.service

[Service]
User=odoo
# Point the bridge at Odoo's own config so it connects to PostgreSQL EXACTLY
# like Odoo (incl. db_host=False -> local unix socket + peer auth, no password).
# This is the robust way — no need to duplicate db host/user/password here.
Environment=ODOO_RC=/etc/odoo.conf
Environment=ODOO_DB=YOUR_DB
Environment=ODOO_ADDONS_PATH=/path/to/addons,/usr/lib/python3/dist-packages/odoo/addons
Environment=ODOO_SERVER_MGMT_KEY=YOUR_FERNET_KEY
Environment=TERM_WS_BIND=127.0.0.1
Environment=TERM_WS_PORT=8770
ExecStart=/usr/bin/python3 /path/to/addons/odoo_server_management/static/ws/terminal_server.py
Restart=always

[Install]
WantedBy=multi-user.target
```
> Run the bridge as the **same OS user as Odoo** (`User=odoo`) so socket/peer DB
> auth works. `ODOO_RC` is strongly preferred over hand-set `ODOO_DB_HOST/USER/
> PASSWORD` — mismatched DB env vars cause `password authentication failed` and
> the terminal closes with an internal error. (Those env vars still work as a
> fallback when no config file is present.)
```bash
systemctl daemon-reload && systemctl enable --now odoo-terminal
```

### 11b. Proxy it (same origin)
In your nginx server block for Odoo:
```nginx
location /terminal/ws/ {
    proxy_pass http://127.0.0.1:8770/;
    proxy_http_version 1.1;
    proxy_set_header Upgrade $http_upgrade;
    proxy_set_header Connection "upgrade";
    proxy_read_timeout 86400s;
}
```
With the proxy in place, leave the `server.terminal.ws_url` parameter empty — the
page defaults to same‑origin `wss://<host>/terminal/ws`. If you do **not** use a
proxy, set the parameter (Settings → Technical → System Parameters):
```
server.terminal.ws_url = ws://<odoo-host>:8770
```

The page loads `xterm.js` from a CDN (jsdelivr) in the admin's browser. The token
is short‑lived and the bridge re‑checks Administrator membership server‑side.

---

## 12. Security summary

- **Key‑only SSH**, no stored passwords; host‑key pinning (TOFU) blocks MITM.
- **Secrets encrypted at rest** (Fernet); the key lives outside the DB.
- **Field‑level** access: passwords/paths/conf are visible to Administrators only,
  even over RPC.
- **Role‑gated** actions enforced server‑side (behind `sudo`), not just in the UI.
- **Logs redacted**: secrets and the discovery payload are stripped from
  `ir.logging`.
- **Private backups** with short‑lived signed URLs.
- **Terminal**: admin‑only, signed short‑lived token, re‑verified by the bridge.

---

## 13. Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `ansible-playbook executable not found` | Install Ansible on the Odoo host, or set `ANSIBLE_PLAYBOOK` env. |
| `Load key ...: invalid format` / `Permission denied (publickey)` | Wrong/empty private key, or the **public** key isn't in the server's `authorized_keys`. Re‑upload the key and install the public part. |
| Restart "succeeds" but nothing happens | SSH user lacks passwordless sudo on the managed server. |
| Empty database/module dropdowns | Run **Discover**; ensure `psql`/`git` exist on the server and the SSH user has sudo. |
| Terminal shows "connecting…" forever | Bridge service not running, or nginx `/terminal/ws/` not proxied, or `server.terminal.ws_url` wrong. |
| Terminal connects then closes / "internal error" / "Terminal authorization failed: cannot reach Odoo database" | Bridge can't reach PostgreSQL — its DB env doesn't match Odoo. Set `Environment=ODOO_RC=/etc/odoo.conf`, run the bridge as `User=odoo`, remove any wrong `ODOO_DB_HOST/PASSWORD`, then `systemctl restart`. Check `journalctl -u odoo-terminal`. |
| Locked out of SSH after `harden_server.sh` (Connection refused on the new port) | sshd bound IPv6-only. From the cloud provider's web console: `ss -tlnp \| grep <port>` — if it shows only `[::]:<port>`, recreate `/etc/systemd/system/ssh.socket.d/port.conf` with `ListenStream=0.0.0.0:<port>` and `ListenStream=[::]:<port>`, then `systemctl daemon-reload && systemctl restart ssh.socket`. |
| Backup fails | `s3cmd` not installed/configured on the managed server, or bucket/region wrong. |
| Blank/white Odoo page after deploy | Regenerate web assets (delete `ir.attachment` asset bundles and reload). |

---

## 14. Quick checklist

- [ ] Ansible + ssh on the Odoo host (`which ansible-playbook ssh`)
- [ ] `ODOO_SERVER_MGMT_KEY` set **and backed up**
- [ ] Global SSH user/port + private key configured; public key on every server
- [ ] Users assigned a role (User / Operator / Administrator)
- [ ] Servers added, **Test Connection** ✅, **Discover** ✅
- [ ] GitHub user + token set (if using Pull)
- [ ] Backup bucket/region set; `s3cmd` configured on servers
- [ ] (Optional) Terminal bridge service + nginx `/terminal/ws/` proxy

# Odoo Server Management — End‑to‑End Setup

Centralized management of multiple Odoo servers from a single Odoo instance:
discover instances, start/stop/restart services, pull code, upgrade modules,
back up databases, stream live logs, and open a real interactive web terminal —
all over **key‑only SSH** with **Ansible**.

> **Module:** `odoo_server_management` (Odoo 16, v1.25) · LGPL‑3
> **Author:** Moutaz Muhammad — <https://github.com/moutazmuhammad>

This README is the **full‑stack, server‑to‑browser** install guide: provision a
fresh Ubuntu host, run Odoo 16 behind nginx with HTTPS, install this module, and
turn on the optional web terminal. For **in‑app configuration** (SSH keys, roles,
discovery, backups, GitHub) see [`DEPLOYMENT.md`](./DEPLOYMENT.md); for day‑to‑day
**usage** see [`USER_GUIDE.md`](./USER_GUIDE.md).

---

## 1. What gets deployed

This is the exact stack running in production (`exp.odex.sa` → `46.101.127.229`):

| Layer | Component | Port / Path |
|---|---|---|
| Edge | **nginx** reverse proxy + Let's Encrypt TLS | `:80` → 301 → `:443` |
| App | **Odoo 16** (gunicorn‑style multi‑worker) | HTTP `127.0.0.1:8016`, gevent `:8072` |
| App | This module in `custom-addons` | — |
| Terminal | `terminal_server.py` WebSocket bridge | `127.0.0.1:8770` |
| Data | **PostgreSQL 16** (local, peer auth) | unix socket |
| Secrets | Fernet key via systemd `Environment=` | — |

```
Browser ──HTTPS:443──► nginx ──► 127.0.0.1:8016  (Odoo HTTP)
                          │  ──► 127.0.0.1:8072  (/websocket, longpolling)
                          │  ──► 127.0.0.1:8770  (/terminal/ws/, PTY bridge)
                          └──► 127.0.0.1:8016  (/log/stream/, SSE, unbuffered)

Odoo host ──key‑only SSH + ansible‑playbook──► managed Odoo servers
```

---

## 2. Versions / prerequisites

Tested on the production host:

- **Ubuntu 24.04 LTS**
- **PostgreSQL 16.14**
- **Python 3.12** (Odoo runs in a venv at `/opt/odoo/venv`)
- **nginx 1.24**
- **certbot 2.9** (apt) with the nginx plugin
- **git 2.43**, **OpenSSH client**, **Ansible** (for remote actions)

Python packages required by the module: `requests`, `PyYAML`, `cryptography`.
Soft/optional: `boto3` (per‑project S3 backups), `paramiko` + `websockets`
(only some terminal setups). System binaries on the Odoo host: `ansible-playbook`,
`ssh`.

---

## 3. Provision the Odoo host (from scratch)

> Skip to §7 if Odoo is already installed and you only need nginx + SSL.

### 3.1 System packages

```bash
sudo apt-get update
sudo apt-get install -y \
  git python3 python3-venv python3-dev build-essential \
  libpq-dev libxml2-dev libxslt1-dev libldap2-dev libsasl2-dev \
  libjpeg-dev zlib1g-dev libfreetype6-dev liblcms2-dev \
  postgresql postgresql-client \
  nginx ansible openssh-client acl \
  node-less                       # optional, for legacy asset compilation
```

### 3.2 PostgreSQL role

Odoo connects locally over the unix socket with **peer auth** (no password):

```bash
sudo -u postgres createuser --createdb odoo
# 'odoo' is the OS user Odoo runs as (created below); peer auth maps OS->DB user
```

### 3.3 Odoo OS user + source

```bash
sudo useradd -m -d /opt/odoo -U -r -s /bin/bash odoo
sudo -u odoo -H bash <<'EOF'
cd /opt/odoo
git clone https://github.com/odoo/odoo --depth 1 --branch 16.0 odoo16
python3 -m venv venv
./venv/bin/pip install --upgrade pip wheel
./venv/bin/pip install -r odoo16/requirements.txt
# module + optional deps
./venv/bin/pip install requests PyYAML cryptography boto3
mkdir -p /opt/odoo/custom-addons /opt/odoo/data
EOF
sudo mkdir -p /var/log/odoo && sudo chown odoo:odoo /var/log/odoo
```

### 3.4 Install this module

```bash
sudo -u odoo cp -r /path/to/odoo_server_management /opt/odoo/custom-addons/
# first install (creates/updates schema), DB name 'mgmt' in production:
sudo -u odoo /opt/odoo/venv/bin/python /opt/odoo/odoo16/odoo-bin \
  -c /etc/odoo.conf -d mgmt -i odoo_server_management --stop-after-init
```

---

## 4. Odoo configuration — `/etc/odoo.conf`

This is the production config. **`proxy_mode = True` is required** so Odoo trusts
the `X-Forwarded-*` headers from nginx (HTTPS detection, real client IP).

```ini
[options]
db_host = False          ; False = local unix socket + peer auth (no password)
db_port = False
db_user = odoo
addons_path = /opt/odoo/odoo16/addons,/opt/odoo/odoo16/odoo/addons,/opt/odoo/custom-addons
data_dir = /opt/odoo/data
http_port = 8016
gevent_port = 8072       ; longpolling / websocket worker
proxy_mode = True        ; REQUIRED behind nginx
logfile = /var/log/odoo/odoo.log
limit_time_cpu = 600
limit_time_real = 1200
limit_memory_soft = 2147483648
limit_memory_hard = 2684354560
workers = 4              ; multi‑worker; set the gevent_port above when workers>0
max_cron_threads = 2
; admin_passwd = <set a strong master password, or manage via the module>
```

> With `workers > 0`, longpolling/websockets move to `gevent_port` (8072) — nginx
> proxies `/websocket` there (see §7).

---

## 5. systemd services

### 5.1 Odoo — `/etc/systemd/system/odoo16.service`

```ini
[Unit]
Description=Odoo 16 (server management)
After=network.target postgresql.service

[Service]
Type=simple
User=odoo
# Fernet key that encrypts secrets at rest (SSH key, GitHub token, master pwds).
# Generate once and BACK IT UP — losing it makes encrypted secrets unrecoverable:
#   python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
Environment=ODOO_SERVER_MGMT_KEY=<YOUR_FERNET_KEY>
ExecStart=/opt/odoo/venv/bin/python /opt/odoo/odoo16/odoo-bin -c /etc/odoo.conf
Restart=always

[Install]
WantedBy=multi-user.target
```

### 5.2 Terminal bridge — `/etc/systemd/system/odoo16-terminal.service`

Only needed for the optional **web terminal**. Run it as the **same OS user as
Odoo** and point it at `/etc/odoo.conf` so it reaches PostgreSQL exactly like Odoo.

```ini
[Unit]
Description=Odoo Server Management - Terminal WS bridge
After=network.target postgresql.service odoo16.service

[Service]
User=odoo
Environment=ODOO_DB=mgmt
Environment=ODOO_DB_HOST=127.0.0.1
Environment=ODOO_DB_USER=odoo
Environment=ODOO_ADDONS_PATH=/opt/odoo/odoo16/addons,/opt/odoo/odoo16/odoo/addons,/opt/odoo/custom-addons
Environment=ODOO_SERVER_MGMT_KEY=<YOUR_FERNET_KEY>
Environment=PYTHONPATH=/opt/odoo/odoo16
Environment=TERM_WS_BIND=127.0.0.1
Environment=TERM_WS_PORT=8770
ExecStart=/opt/odoo/venv/bin/python /opt/odoo/custom-addons/odoo_server_management/static/ws/terminal_server.py
Restart=always

[Install]
WantedBy=multi-user.target
```

Optional drop‑in `/etc/systemd/system/odoo16-terminal.service.d/override-db.conf`
(preferred over hand‑set DB vars — makes the bridge read Odoo's own config):

```ini
[Service]
Environment=ODOO_DB=mgmt
Environment=ODOO_RC=/etc/odoo.conf
```

Enable both:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now odoo16.service odoo16-terminal.service
```

---

## 6. DNS

Point the domain at the host **before** requesting a certificate:

```
exp.odex.sa.   A   46.101.127.229
```

Verify it has propagated: `dig +short exp.odex.sa` → `46.101.127.229`.
(`www` is optional — add a separate `A`/`CNAME` and include `-d www.exp.odex.sa`
in §8 if you want it.)

---

## 7. nginx reverse proxy — `/etc/nginx/conf.d/odoo-mgmt.conf`

Drop this in (HTTP‑only first; certbot adds the TLS bits in §8). It proxies the
main app, the websocket/longpolling worker, the live‑log SSE stream (unbuffered),
the terminal bridge, and caches static assets.

```nginx
# WebSocket connection upgrade map
map $http_upgrade $connection_upgrade {
    default upgrade;
    ''      close;
}

upstream odoo {
    server 127.0.0.1:8016;
    keepalive 32;
}

server {
    listen 80;
    server_name exp.odex.sa;

    access_log /var/log/nginx/odoo.access.log;
    error_log  /var/log/nginx/odoo.error.log;

    # Uploads (DB restore, backups can be large)
    client_max_body_size 500M;
    client_body_buffer_size 256k;
    client_body_timeout 600s;

    # Proxy defaults — these headers make proxy_mode=True work correctly
    proxy_http_version 1.1;
    proxy_redirect off;
    proxy_set_header Host               $host;
    proxy_set_header X-Real-IP          $remote_addr;
    proxy_set_header X-Forwarded-For    $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto  $scheme;
    proxy_set_header X-Forwarded-Host   $host;
    proxy_set_header X-Forwarded-Port   $server_port;
    proxy_connect_timeout 60s;
    proxy_send_timeout    900s;
    proxy_read_timeout    900s;
    send_timeout          900s;

    gzip on;
    gzip_vary on;
    gzip_comp_level 5;
    gzip_types text/plain text/css text/xml application/json
               application/javascript application/xml application/rss+xml
               image/svg+xml;

    # Odoo websocket / longpolling (workers > 0 → gevent_port 8072)
    location /websocket {
        proxy_pass http://127.0.0.1:8072;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection $connection_upgrade;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_read_timeout 86400s;
        proxy_send_timeout 86400s;
    }

    # Web terminal PTY bridge (optional feature)
    location /terminal/ws/ {
        proxy_pass http://127.0.0.1:8770/;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_read_timeout 86400s;
    }

    # Live log streaming (Server‑Sent Events) — must NOT be buffered
    location /log/stream/ {
        proxy_pass http://odoo;
        proxy_buffering off;
        proxy_request_buffering off;
        proxy_cache off;
        proxy_set_header X-Accel-Buffering no;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_read_timeout 86400s;
        proxy_send_timeout 86400s;
    }

    # Static assets — cache aggressively
    location ~* /web/static/ {
        proxy_pass http://odoo;
        expires 30d;
        proxy_cache_valid 200 90m;
        add_header Cache-Control "public";
    }

    # Everything else → Odoo
    location / {
        proxy_pass http://odoo;
        proxy_connect_timeout 60s;
        proxy_send_timeout 900s;
        proxy_read_timeout 900s;
    }
}
```

Apply it:

```bash
sudo nginx -t && sudo systemctl reload nginx
# sanity check the app answers through nginx:
curl -I -H 'Host: exp.odex.sa' http://127.0.0.1/web/login   # expect 200
```

---

## 8. HTTPS with Let's Encrypt (certbot)

With DNS pointing at the host (§6) and nginx serving the domain on port 80 (§7):

```bash
sudo apt-get install -y certbot python3-certbot-nginx

sudo certbot --nginx -d exp.odex.sa \
  --non-interactive --agree-tos \
  --email you@example.com \
  --redirect
```

certbot edits the vhost in place: it adds the `listen 443 ssl` block, wires the
certificate paths, and creates a second `server {}` that **301‑redirects all HTTP
to HTTPS**. The result is exactly the live config:

```nginx
server {
    server_name exp.odex.sa;
    # ... all the location blocks from §7 ...

    listen 443 ssl;                                              # managed by Certbot
    ssl_certificate     /etc/letsencrypt/live/exp.odex.sa/fullchain.pem;  # managed by Certbot
    ssl_certificate_key /etc/letsencrypt/live/exp.odex.sa/privkey.pem;    # managed by Certbot
    include /etc/letsencrypt/options-ssl-nginx.conf;             # managed by Certbot
    ssl_dhparam /etc/letsencrypt/ssl-dhparams.pem;               # managed by Certbot
}

server {
    if ($host = exp.odex.sa) { return 301 https://$host$request_uri; }  # managed by Certbot
    listen 80 default_server;
    server_name exp.odex.sa;
    return 404;                                                  # managed by Certbot
}
```

**Auto‑renewal** is installed as a systemd timer (`certbot.timer`); the cert
renews automatically ~30 days before expiry. Verify:

```bash
systemctl list-timers | grep certbot      # timer is active
sudo certbot renew --dry-run               # end‑to‑end renewal test
```

Confirm from outside:

```bash
curl -I https://exp.odex.sa/web/login      # 200 OK
curl -I http://exp.odex.sa/web/login       # 301 → https://...
```

---

## 9. Server hardening (recommended)

The repo ships [`harden_server.sh`](../harden_server.sh) (run as root on **each**
host). It:

1. Makes your SSH public key the **only** authorized key (root + a named user).
2. Strips any embedded GitHub tokens from git config / credential files.
3. Moves SSH to **port 7812** and **disables password auth** (key‑only).
4. Removes any leftover S3/Spaces credentials and standalone backup crons
   (this module's daily backups upload via short‑lived pre‑signed URLs — no keys
   on managed servers).

> Edit the `PUBKEY` at the top of the script to your real public key **before**
> running — it refuses to disable password login while the placeholder is present,
> so you can't lock yourself out. On Ubuntu 22.10+/24.04 (socket‑activated SSH) it
> binds **both** IPv4 and IPv6 on the new port to avoid an IPv6‑only lockout.

After hardening, connect with: `ssh -p 7812 <user>@<host>`.

---

## 10. In‑app configuration (next steps)

Once the site is up, finish configuration **inside Odoo** — full details in
[`DEPLOYMENT.md`](./DEPLOYMENT.md):

1. **Encryption key** — `ODOO_SERVER_MGMT_KEY` set and backed up (§5.1).
2. **Global SSH key** — Server Management → GitHub Configuration → SSH: set the
   default SSH user/port and upload the **private** key; install its **public**
   key on every managed server.
3. **Roles** — assign each user *User / Operator / Administrator* (Settings → Users).
4. **Servers** — add IP + port, **Test Connection**, then **Discover Instances**.
5. **GitHub** — username + PAT (`repo` scope) for "Pull Code".
6. **Backups** — per‑project DigitalOcean Spaces / S3 keys; assign projects to servers.

Then use the module per [`USER_GUIDE.md`](./USER_GUIDE.md).

---

## 11. Operations cheat‑sheet

```bash
# service status / logs
sudo systemctl status odoo16 odoo16-terminal nginx
sudo journalctl -u odoo16 -f
tail -f /var/log/odoo/odoo.log
tail -f /var/log/nginx/odoo.error.log

# restart after config / code change
sudo systemctl restart odoo16
sudo nginx -t && sudo systemctl reload nginx

# update the module after pulling new code
sudo -u odoo /opt/odoo/venv/bin/python /opt/odoo/odoo16/odoo-bin \
  -c /etc/odoo.conf -d mgmt -u odoo_server_management --stop-after-init
sudo systemctl restart odoo16

# what's listening
sudo ss -tlnp | grep -E ':80|:443|:8016|:8072|:8770'
```

---

## 12. Troubleshooting

| Symptom | Cause / fix |
|---|---|
| HTTPS redirect loop or "not secure" mixed content | `proxy_mode = True` missing in `/etc/odoo.conf`, or nginx not forwarding `X-Forwarded-Proto`. |
| `502 Bad Gateway` | Odoo not running / wrong upstream port. Check `systemctl status odoo16` and `http_port=8016`. |
| Websocket / longpolling fails (chat, live updates) | `/websocket` not proxied to `:8072`, or `workers = 0`. Ensure `gevent_port` is set and proxied. |
| Live logs never stream | `/log/stream/` is buffered. Keep `proxy_buffering off` + `X-Accel-Buffering no` + long `proxy_read_timeout`. |
| Terminal stuck "connecting…" | Bridge not running, `/terminal/ws/` not proxied, or `server.terminal.ws_url` wrong. `journalctl -u odoo16-terminal`. |
| Terminal closes / "cannot reach Odoo database" | Bridge DB env ≠ Odoo. Run as `User=odoo`, set `Environment=ODOO_RC=/etc/odoo.conf`, drop wrong `ODOO_DB_*`, restart. |
| `certbot` fails the HTTP‑01 challenge | DNS not pointing at this host yet, or port 80 blocked. Confirm `dig +short exp.odex.sa` and that nginx serves `:80`. |
| Locked out after `harden_server.sh` (refused on new port) | sshd bound IPv6‑only. From the provider console set `ListenStream=0.0.0.0:7812` **and** `[::]:7812` in `/etc/systemd/system/ssh.socket.d/port.conf`, then reload. |
| Blank/white page after deploy | Regenerate web assets (clear the asset `ir.attachment` bundles, reload). |

See [`DEPLOYMENT.md`](./DEPLOYMENT.md) §13 for the app‑level troubleshooting table.
```

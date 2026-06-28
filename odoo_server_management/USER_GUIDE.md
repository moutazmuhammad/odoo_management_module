# Odoo Server Management — Feature Guide & How‑To

Centrally manage many Odoo servers over SSH from one Odoo instance ("the manager"):
onboard servers by IP, auto‑discover their Odoo instances, start/stop/restart
services, pull code, upgrade modules, view live logs, open a web terminal, and run
encrypted off‑site database backups — with role‑based access and secrets encrypted
at rest.

---

## 1. Concepts

| Term | Meaning |
|------|---------|
| **Manager** | The Odoo instance where this module runs and from which everything is controlled. |
| **Server (Host)** | A physical/virtual machine reached over SSH. You add it by **IP only**. |
| **Stage (Instance)** | One Odoo service running on a server (auto‑detected). A server can have many. |
| **Client Server** | A stage flagged as an external customer instance — actions on it are restricted to Operator/Admin. |
| **Shared Space** | One object‑storage bucket (DigitalOcean Spaces / S3) holding all backups. |

### Roles (who can do what)
Roles are hierarchical — each includes the ones below it.

| Role | Can do |
|------|--------|
| **User** | Act on **Stages**: Check Status, View Logs, View Conf, Backup, Pull Code, Upgrade Module, Start/Stop/Restart. Cannot see servers, passwords, paths, or settings. |
| **Operator** | Everything a User can, **plus** see/add **Servers**, run **Test Connection** and **Discover**, **Run Backup Now**, act on **Client Server** stages. |
| **Administrator** | Everything, **plus** see sensitive details (passwords, conf, paths), edit **General Settings**, open the **web Terminal**, install **backup agents**, manage **auto‑stop**. |

Assign roles in **Settings → Users** (the "Project Roles" category).

---

## 2. Navigation

```
Server Management
├── Repository Management        (Admin)
│   ├── General Settings         ← all global config (SSH, GitHub, Backups, Auto‑Stop, Signup)
│   ├── Git Repositories         ← repos auto‑discovered on your servers
│   └── Stage Repo Paths         ← map repo+branch+path to a stage (for Pull Code)
├── Servers                      (Operator) ← the machines you manage
└── Stages                       (User)     ← the Odoo instances on those machines
```

---

## 3. First‑time setup (Administrator)

Open **Server Management → General Settings**:

1. **SSH (global key, key‑auth only):**
   - Set **Default SSH User** and **Port**.
   - **Upload** your private key (preferred) or paste it. It is validated, encrypted at
     rest, and written to a `0600` file on the manager.
   - The matching **public key must already be installed** on every managed server
     (`~<user>/.ssh/authorized_keys`), and that user needs passwordless `sudo`.
2. **GitHub** (only if you use Pull Code): set **GitHub Username** and a **Token** (PAT
   with `repo` scope). Write‑only — blank means "keep current".
3. **Backups** (see §8): bucket, region, access/secret keys, then click **Test Storage**.
4. **Auto‑Stop / Signup**: optional (see §9, §10).

> **Encryption key:** secrets are encrypted with Fernet. The key comes from the
> `ODOO_SERVER_MGMT_KEY` env var (recommended) or an auto‑generated `0600` file under
> the data dir. **Back it up** — if lost, stored secrets can't be decrypted.

---

## 4. Onboard a server (Operator)

**Servers → Create:**
1. Enter a **Name**, the **IP/hostname**, and the **SSH Port**. Save.
2. Click **Test Connection** → verifies the SSH key works (sets "Connection Verified").
3. Click **Discover Instances** → the manager SSHes in and auto‑detects every Odoo
   service: service name, version, conf file, log path, HTTP port, domain, master
   password, custom modules, and git repos. One **Stage** is created per service.
4. **Check Status** refreshes each instance's running/stopped state.

Other server buttons:
- **Open Terminal** (Operator+) — a real web SSH console for the host (see §7).
- **Run Backup Now** (Operator+) — back up all the server's live databases immediately.
- **Install Self‑Backup Agent** (Admin) — make the server back itself up (see §8.3).

Server form also shows **Backup Category** (erp/odex), **Additional Databases**
(backup overrides), **Last Daily Backup**, and the **Detected Instances** list.

---

## 5. Manage an instance (Stage)

Open a stage from **Stages**, or from a server's **Detected Instances** list via the
**Open / Backups** button. Header actions (a 🔒 means hidden for non‑Operators on
*Client Server* stages):

| Button | What it does |
|--------|--------------|
| **Check Status** | Probes the instance's HTTP endpoint; updates 🟢/🔴/⚪. |
| **Start / Stop / Restart** 🔒 | `systemctl` the service. |
| **View Logs** | Live log stream (tails the log file over SSH, in‑browser). |
| **View Conf File** | Shows the Odoo `.conf` with sensitive keys redacted. |
| **Pull Code** 🔒 | Git‑pull a linked repo/branch (GitHub token injected; runs as the Odoo user). |
| **Upgrade Module** 🔒 | Stop → `-u <module> -d <db> --stop-after-init` → restart. |
| **Backup** 🔒 | On‑demand backup of one database (see §8.2). |

**Notebook pages:** General Info; Access Info (Admin — master password, OS user);
Branch Configuration (map repos/branches for Pull Code); **Backups** (this stage's
stored backups with Download — see §8.4).

The instance's **database list** is cached and refreshed automatically every 15 min
(and just before each nightly backup), so the Backup/Upgrade pickers open instantly.

---

## 6. Pull Code & Upgrade Module

**Pull Code:** First, under a stage's **Branch Configuration** (or **Stage Repo
Paths**), link a repository + branch + target path. Then **Pull Code** → pick that
mapping → it `git stash` + `git pull`s as the Odoo user using your GitHub token.

**Upgrade Module:** **Upgrade Module** → pick a **Database** and a **Module** (lists
come from discovery) → the service is stopped, the module upgraded, and the service
restarted.

---

## 7. Web Terminal & Live Logs

- **Web Terminal** (Operator+): **Servers → Open Terminal** opens an `xterm.js`
  console. Access uses a short‑lived (300 s) HMAC‑signed token. It needs the terminal
  **bridge** service running and proxied (see DEPLOYMENT.md). If the bridge isn't
  behind nginx, set `server.terminal.ws_url` to its `ws://host:port`.
- **Live Logs** (User+): **View Logs** streams the instance's log file via SSE
  (SSH `tail -f`). Sessions auto‑end after 10 minutes; reload to resume.

---

## 8. Backups

All backups go to **one shared Space**. The manager holds the Spaces key (encrypted);
**servers never store it** — they upload using short‑lived pre‑signed URLs.

**Object layout:**
```
<prefix?>/<category>/<ip-or-domain>/<db>/<db>_<date>.zip   ← daily
<prefix?>/manual/<category>/<ip-or-domain>/<db>.<ext>      ← on-demand
```
`category` is the server's **erp/odex** selection.

### 8.1 Configure storage (Admin)
General Settings → **Backups**: set **Bucket**, **Region** (e.g. `fra1`), optional
**Endpoint/Prefix**, **Retention (days)**, **Download Link TTL**, and the **Access/Secret
Key**. Click **Test Storage** → expect `✅ Connected to Space '…'`. Toggle **Daily
Backups Enabled** and set the **Daily Backup Hour** (server/UTC time).

### 8.2 On‑demand backup (User+)
On a stage: **Backup** → choose a **Database** and **Format** (Zip = DB + filestore,
Dump = SQL only) → it dumps on the server, uploads to `manual/…`, and gives you a
download link. The `manual/` area is wiped daily at 03:00 (latest‑only).

### 8.3 Daily backups — two modes
- **Manager‑driven (default):** the hourly job backs up every server during the
  configured hour. The manager picks **only the exposed/live database per stage**
  (same selection the Upgrade picker uses: owned by the conf's DB user, narrowed by
  `db_name`/`dbfilter`) — old/duplicate copies are skipped. For multi‑DB or ambiguous
  stages, list exact names in the server's **Additional Databases to Back Up**.
- **Self‑backup agent (decentralized):** **Install Self‑Backup Agent** (or leave
  **Auto‑install Backup Agent** on so every reachable server gets it within the hour).
  Each server then runs a local daily cron that asks the manager for upload URLs and
  uploads itself — near‑zero manager load. The server stores only a low‑privilege
  token (not the Spaces key). The manager stops backing up agent‑managed servers.

Both modes handle **any DB size** (streaming dump → multipart upload), remote or local
PostgreSQL (picking a `pg_dump` matching the server version), and back up each DB in
its own run so one failure doesn't stop the rest.

### 8.4 Browse & download stored backups
Open a stage → **Backups** page → see its backups (DB, file, type, size, date) →
**Download** for a temporary link. Backups of **Client Server** stages can only be
downloaded by Operator/Admin.

---

## 9. Auto‑Stop idle instances (Admin)
Stop dev instances left running too long. In General Settings set **Auto‑Stop … (days)**.
On a **Server**, enable **Stop Instances**; on each **Stage**, tick **Auto‑Stop**. A
daily job stops instances whose service has run longer than the configured days. Set
days to `0` to disable.

---

## 10. User self‑signup (Admin)
General Settings → **User Signup**: enable self‑registration and optionally restrict to
**Allowed Email Domains** (e.g. `exp-sa.com, odex.sa`). New sign‑ups get the **User**
role only.

---

## 11. Scheduled jobs (crons)
| Job | Schedule | Purpose |
|-----|----------|---------|
| Refresh database lists | every 15 min | Cache each instance's DB list. |
| Refresh instance status | every 15 min | Update 🟢/🔴 status. |
| Daily database backups | hourly (acts in the set hour) | Back up non‑agent servers; prune old objects. |
| Purge manual backups | daily 03:00 | Empty the `manual/` area. |
| Ensure backup agents installed | hourly | Auto‑install the agent on new/reachable servers. |
| Auto‑stop stale instances | daily | Stop long‑running instances (where enabled). |

---

## 12. Security model
- **Key‑only SSH** with host‑key pinning (TOFU); one global key on the manager.
- **Secrets encrypted at rest** (Fernet) — SSH key, GitHub token, Spaces keys, master
  passwords; the key lives outside the database.
- **No cloud keys on servers** — only short‑lived pre‑signed URLs, or a per‑host
  low‑privilege agent token scoped to that host's own backup prefix.
- **Field‑level access** — passwords/paths/conf are Admin‑only, enforced server‑side.
- **Logs redacted** — secrets and discovery payloads are stripped before logging.

---

## 13. Requirements (summary)
**Manager:** `ansible-playbook` + `ssh` on PATH; Python `requests`, `PyYAML`,
`cryptography`; `boto3` if using backups; `paramiko`/`websockets` only for the web
terminal bridge.
**Each managed server:** Linux + systemd; an SSH user with passwordless `sudo` and the
manager's public key; `python3`, `git`, `acl`, `curl`, and `psql`/`pg_dump` matching the
PostgreSQL server version.

See `DEPLOYMENT.md` for full installation and the terminal‑bridge/nginx setup.

# Per-Project Daily Backups — Design Plan

## Goal (from requirements)
- Multiple **DigitalOcean projects**, each with its **own** access key, secret key,
  and **bucket** (and region/endpoint).
- Each **Server** is assigned to one project → all its services back up to that
  project's bucket. *(decision: per-server assignment)*
- A **daily** automatic backup of **every database of every service** on a server,
  stored in the project's bucket. *(decision: one schedule per server, all DBs,
  fixed time ~02:00, prune by retention)*
- This is **separate** from the real-time **button**, which keeps using the
  existing single global bucket. *(decision: button unchanged)*
- **No S3 credentials (and no long-lived secrets) stored on the managed servers.**
  *(decision: must be avoided — this drives the architecture below)*

## The credential problem & the solution: pre-signed PUT URLs
`s3cmd` on the server needs `~/.s3cfg` (the secret key) — not allowed. Instead:

- The **per-project keys live only in Odoo**, encrypted at rest (Fernet), exactly
  like the SSH key / master passwords already are.
- At backup time, **Odoo** (which holds the keys) generates a short-lived
  **pre-signed S3 PUT URL** for the exact object key. The server uploads with a
  plain `curl -T file "<presigned-url>"` — the URL carries a temporary signature,
  so **no key ever touches the server**.
- **Retention/pruning** is done from Odoo (it lists+deletes old objects with the
  project keys). The server never lists or deletes.
- The dump itself is produced exactly like the real-time button (curl Odoo's
  `/web/database/backup` with the service master password passed **ephemerally**
  via Ansible `no_log` — never written to disk on the server).

Net: the only things that ever reach the server are (a) the ephemeral master
password (already the case today) and (b) a single-use, time-limited upload URL.
No persistent secrets.

## Architecture choice (need your pick)

### Design A — Odoo-orchestrated (RECOMMENDED)
- A daily Odoo scheduled action (`ir.cron`) loops over servers → services → DBs.
- For each DB: Odoo mints a presigned PUT URL, then runs an Ansible playbook on
  the server that dumps + uploads via that URL. Odoo prunes old objects after.
- **Pros:** simplest; nothing persistent on the server; central control, logging,
  and retention; no inbound endpoint; reuses today's SSH/Ansible plumbing.
- **Cons:** backups run from the Odoo host's schedule (if Odoo is down at 02:00,
  that night is skipped — same reliability as every other action in this module).

### Design B — Server cron + callback (only if you need server autonomy)
- A real `cron` on each server runs a script daily. Each run it calls back to a
  new authenticated Odoo endpoint (per-server capability **token**, *not* the S3
  keys) to fetch fresh presigned PUT URLs, then dumps + uploads.
- **Pros:** literally a "linux cron on each server"; runs even if Odoo's own cron
  is disabled (as long as Odoo HTTP is reachable at run time).
- **Cons:** much more to build/secure — a new public callback endpoint, a signed
  per-server token, a server-side script + cron, and scope checks so a server can
  only write to its own project prefix. Still needs Odoo reachable each run.

> Both honor "no S3 creds on the server." A is smaller, safer, easier to operate;
> B matches the literal "cron on each server" wording at real added complexity.

## Data model (both designs)
- **New model `server.backup.project`** (a DO project / Spaces target):
  - `name` (required)
  - `region` (e.g. `nyc3`) → endpoint `https://<region>.digitaloceanspaces.com`
  - `bucket` (required)
  - `access_key_enc`, `secret_key_enc` — encrypted at rest; write-only in the form
    (mirrors the `admin_password_enc` pattern in `stage.py`)
  - `prefix` (default `DAILY`)
  - `retention_days` (default 7)
  - `daily_backup_enabled` (bool)
  - `keys_set` (computed, for the UI)
  - admin-only access (group_admin), like other secrets.
- **`server.host` += `backup_project_id`** Many2one (+ a "Daily Backups" smart
  indicator). All services on the host use this project.

## Object key layout (per your spec)
`s3://<bucket>/<server-name>/<domain-or-ip>/<database>.zip`
- `<server-name>`: `server.host.name` slugified — lowercased, runs of
  spaces/symbols → single `-`. "Dev Server" → `dev-server`.
- `<domain-or-ip>`: the service's domain if it has one (e.g. `exp.odex.sa`),
  else the host IP with dots → dashes and port dropped
  (`151.251.416.152:8069` → `151-251-416-152`).
- `<database>.zip`: database name + format extension.
- DECIDED: keep N days of history → final key
  `<bucket>/<server-slug>/<domain-or-ip>/<database>/<YYYY-MM-DD>.zip`.
  Project `retention_days` prunes older dated objects (by S3 LastModified).
- DECIDED: **Design A** (Odoo-orchestrated cron + pre-signed PUT URLs).

## Backup mechanism (smart, self-detecting, Odoo-format)
A single smart script `ansible/playbooks/files/smart_backup.py` is pushed to each
server every run (so it auto-updates and always sees new services/DBs). It runs
as the Odoo OS user (`sudo -u <odoo user>`, peer auth) and has two modes:
- `detect` → prints JSON `[{db, domain, filestore}]` for **every Odoo DB** on the
  host (lists DBs via `psql`, keeps those with `ir_module_module`, reads each
  DB's own `web.base.url` for the domain segment, locates its filestore).
- `backup <mapfile>` → `mapfile` is `{db: presigned_put_url}`. For each DB it
  builds an **Odoo-identical** zip and `curl -T`s it to the URL.

The zip matches `odoo/service/db.dump_db('zip')` exactly so it restores via Odoo's
DB manager:
- `dump.sql` = `pg_dump --no-owner <db>` (plain SQL)
- `filestore/...` = copy of `<data_dir>/filestore/<db>`
- `manifest.json` = `{odoo_dump, db_name, version, version_info, major_version,
  pg_version, modules{name:version}}` built from `ir_module_module` + `SHOW
  server_version`.

Daily flow (Design A, per host): push script → `detect` → Odoo presigns one PUT
URL per DB (`<server>/<domain-or-ip>/<db>/<date>.zip`) → write the URL map to the
server (0600, temp) → `backup` → remove map → Odoo prunes objects older than
`retention_days`. No S3 keys ever leave Odoo; the master password is no longer
needed at all (pg_dump replaces the HTTP endpoint).

## New / changed files
- `models/backup_project.py` — new model + Fernet-encrypted key fields + boto3
  helpers `_presign_put(key, ttl)` and `_prune(prefix, retention_days)`.
- `models/server_host.py` — add `backup_project_id`.
- `models/stage.py` — reuse encryption helpers (already there).
- `ansible/playbooks/daily_backup_upload.yml` — dump (curl Odoo endpoint, no_log
  master pw) → `curl -T` to the presigned URL → rm. No creds.
- `data/ir_cron.xml` — add daily `_cron_daily_backups` (Design A).
- `views/backup_project_views.xml` + menu + `security/ir.model.access.csv`.
- `views/server_host_views.xml` — add the project field.
- `__manifest__.py` — register views; add `boto3` to external python deps.
- `DEPLOYMENT.md` — document projects + `pip install boto3` + how presigned
  uploads avoid server-side credentials.

## Dependency
- **boto3** on the Odoo host (for presigned URLs + prune). Standard, light.
  *(Alternative: hand-rolled SigV4 presigning with hmac/hashlib to avoid the dep —
  more code; only if you'd rather not install boto3.)*

## Out of scope / unchanged
- The real-time **backup button** and the existing global bucket settings stay
  as-is.
- DB-list cache cron, auto-stop cron, etc. unchanged.

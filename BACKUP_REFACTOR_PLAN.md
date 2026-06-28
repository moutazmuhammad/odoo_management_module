# Backup Refactor + ERP Fix — Plan

## Why ERP backups never ran (root cause)
- ERP server `134.209.166.14` has **no local PostgreSQL**. Its 5 Odoo instances
  (`socpadev`, `socpapreprod`, `takafoldev`, `trasol_dev`, `trasol_preprod`)
  connect to a **remote managed Postgres** at `10.108.0.11:5219` (per
  `/etc/odoo/*.conf`: `db_host`/`db_port`/`db_user`/`db_password`).
- `smart_backup.py` only ever spoke to a **local** Postgres (peer auth via the
  `postgres` OS user / Unix socket). On ERP that user doesn't exist and the
  socket isn't there → `detect` returns `[]` → backup is a silent no-op →
  `last_backup` stays empty.
- Odex `138.197.188.136` has a local Postgres with peer auth → works.

## Decisions (confirmed with user)
1. **Single shared Space** for all servers. Bucket + credentials + region live
   **globally** in Settings (menu renamed **GitHub Configuration → General
   Settings**). The `server.backup.project` model is removed.
2. **Required selection on the server**: `backup_category` ∈ {`odex`, `erp`}.
3. **Object key layout**: `<prefix?>/<category>/<ip-or-domain>/<db>/<db>_<date>.zip`.
4. **ERP fix**: teach `smart_backup.py` to read each Odoo conf and dump over the
   remote DB connection. Backups still run **on the client server** (dump + zip +
   pre-signed upload); the manager never touches the data or the creds.

## Changes
- **NEW** `models/backup_storage.py` — `server.backup.storage` (AbstractModel):
  all boto/presign/multipart/prune/purge logic, reading global config params
  (`server.backup.bucket|region|endpoint|prefix|retention_days|signed_url_ttl|
  daily_enabled|access_key|secret_key`; keys encrypted at rest).
- **DELETE** `models/backup_project.py`, `views/backup_project_views.xml`.
- `models/server_host.py`: drop `backup_project_id`; add required
  `backup_category`; `_run_daily_backup` + `_cron_daily_backups` use the global
  storage; new `action_run_backup_now` button. Path uses `<db>_<date>.zip`.
- `models/server_backup_database_wizard.py`: manual backup uses global storage,
  key `manual/<category>/<ip-or-domain>/<db>.<ext>` (overwrite-in-place).
- `models/github_settings.py` + `views/github_settings.xml`: rename to General
  Settings; add Backups (storage) section + "Test Storage" button.
- `data/ir_cron.xml`: purge-manual cron now targets `server.backup.storage`.
- `security/ir.model.access.csv`: drop the `server.backup.project` line.
- `__manifest__.py`: drop project view, bump version → `1.2`.
- `migrations/1.2/post-migration.py`: set `backup_category` from the old
  project name, drop the orphan `backup_project_id` column.
- `ansible/playbooks/files/smart_backup.py`: **remote-DB support** — parse all
  Odoo confs, build per-source connections (local peer OR remote `-h/-p/-U` +
  `PGPASSWORD`), enumerate DBs via `db_name`/`dbfilter`, and dump each with the
  right connection. Local (Odex) path unchanged.

## Deploy — DONE (2026-06-27)
1. ✅ Synced module to `/opt/odoo/custom-addons/odoo_server_management` (manager
   46.101.127.229, db `mgmt`, services `odoo16` + `odoo16-terminal`).
2. ✅ Global storage = single Space **`erp-servers-backup`** (fra1), creds copied
   from old project 2 into `server.backup.*` ir.config_parameter (encrypted).
3. ✅ Upgraded to 1.2; migration set `backup_category` (host1=odex, host2=erp)
   and dropped the old project table/column.
4. ✅ Verified end-to-end: ERP db uploaded to
   `erp/interhr.dev.exp-sa.com/db_hr_test_05012024/db_hr_test_05012024_2026-06-27.zip`.

## Extra fixes found during deploy
- **pg_dump version mismatch** (the real dump-stage blocker): ERP's remote PG is
  **16.9** but the client only had pg_dump **14** (refuses to dump newer server).
  Fix: `smart_backup.py` now auto-selects a `pg_dump` whose major ≥ the server's
  (searches `/usr/lib/postgresql/*/bin`), and **postgresql-client-16** was
  installed on the ERP server (134.209.166.14).
- **Scale hardening** (large #servers/#dbs): each DB now backs up in its OWN
  ansible run (own 6 h timeout, partial progress persists, temp disk bounded to
  one DB on `/var/tmp`). Daily cron commits per host. Streaming dump→zip +
  multipart upload handle 30 GB+ DBs.

## Exposed-only selection + overrides (v1.3, deployed 2026-06-28)
- Goal: back up only the live DB each stage exposes, not old/duplicate copies.
- `smart_backup.py detect` now enumerates the SAME way as the Upgrade Module
  button (DBs owned by the conf's db_user + db_name/literal dbfilter), then picks
  the single canonical DB per stage: **db_name → dbfilter stem → unique match**.
  Ambiguous stages (several matches, no clear stem) are SKIPPED and reported via
  `ODOO_BACKUP_SKIPPED:` (diagnostic; ignored by the parser).
- Preview on ERP: **84 owned → 44 backed up**; old/dated copies dropped
  (`takafol_dev_db` not `_6_10_2024`; one `odex25_std_dev_db` not 5 copies).
- 7 ambiguous instances (one-live-plus-copies AND a genuine multi-DB demo server
  serving 8 DBs) are handled by a per-host override:
  `server.host.backup_extra_dbs` ("Additional Databases to Back Up", admin) — a
  comma/newline list of exact names passed to detect via `force_dbs_b64` and
  force-included. Tested: a force-included DB uploaded to `expert-db-backups`.
- Storage now points at the user's single Space **`expert-db-backups`** (fra1).
- Fixed **Test Storage** AccessDenied: it no longer calls ListBuckets (account-
  level); it checks the target Space directly (list_objects). Verified OK.
- Constraint honored: NO changes to stage servers in this phase (only read-only
  probes, cleaned up). EARLIER one-off change still in place: postgresql-client-16
  installed on the ERP stage (needed for pg16 dumps) — confirm keep vs revert.

## Open item for the user
- ERP host has **~60 databases** incl. many old/duplicate copies and very large
  ones (one ~211 GB; total ~1 TB). A full daily run is large; with 7-day
  retention that multiplies. Consider scoping (drop stale `_backup/_dup/_old`
  copies, tighten each instance's `dbfilter`, or add an allow/deny list).
  `_run_daily_backup(only_dbs=[...])` already supports scoped runs internally.

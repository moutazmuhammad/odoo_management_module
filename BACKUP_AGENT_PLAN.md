# Decentralized backup agent (presign-on-demand) — Plan

## Goal (from user)
- Backups run **on each server**, triggered by a **local Linux cron** (not the
  manager) → near-zero manager load, parallel, resilient trigger.
- **No storage key stored on the servers.** A credential may exist on the server
  **only during the backup**, then disappear.
- DB list refreshes itself (already true: agent self-detects each run).

## Key realization
"Credential only during the backup" == **pre-signed URLs minted on demand**.
The Spaces secret key lives ONLY on the manager (encrypted). At backup time the
agent asks the manager for short-lived upload URLs, uploads directly to the
Space, and the URLs expire. The only persistent secret on the server is a
**low-privilege agent token** (NOT a cloud key): it can only request upload URLs
for that server's own prefix — it cannot read/delete other backups or touch the
bucket directly.

Trade-off (unavoidable): the manager must be reachable for the brief presign
call. If it's down during the window, the agent retries through the day; only a
full-window outage skips that night. (True offline backup is impossible without a
real key on the server, which the user ruled out.)

## Architecture
**Manager = control plane + presign service (light).** Servers = data plane.

1. **Manager HTTP endpoints** (new controller, token-authenticated, JSON):
   - `POST /server_backup/presign` — body: {token, dbs:[{db,size,domain}]}.
     Validates token→host, builds object keys under the host's
     `<category>/<domain-or-ip>/<db>/<db>_<date>.zip`, returns per-db single PUT
     or multipart (upload_id + part_urls). Reuses `server.backup.storage`.
   - `POST /server_backup/finalize` — body: {token, results}. Completes/aborts
     multipart uploads, prunes old objects, stamps `last_backup`, stores status.
   - Auth: per-host random token (`server.host.agent_token`, admin/0600 on
     server). Endpoint enforces keys stay within the host's own prefix.
2. **Agent on each server** (`/opt/odoo-backup/agent.py` = smart_backup.py + a
   thin `auto` runner): detect exposed DBs locally (canonical + extra_dbs) →
   call `/presign` → dump+zip+upload via the returned URLs → call `/finalize`.
   Config `/etc/odoo-backup.conf` (0600): manager URL + agent token + hour.
   Cron `/etc/cron.d/odoo-backup` daily at the hour, with jitter + intra-day
   retry until success.
3. **Deploy from the manager** (reuse existing SSH): a "Deploy backup agent"
   action installs agent+config+cron on a host and writes its token. A light
   daily sync updates the agent/config if changed. The manager's own backup
   cron is disabled (agents own the schedule) to avoid double backups.
4. **Central visibility kept**: `/finalize` records last_backup + per-db status,
   shown in the host form (so we don't lose the dashboard).

## Security notes
- Spaces key: manager only, encrypted (unchanged).
- Server holds only the agent token (least privilege, per-host, revocable).
- Presign endpoint validates the requested keys are within the host's prefix, so
  a stolen token can only write that host's own backups.
- Multipart completion done by the manager (agent never needs list/complete creds).

## Rollout
- Build + unit-test the endpoints and agent locally.
- **Pilot on ERP (134.209.166.14)** end-to-end on its own cron; verify objects
  land in `expert-db-backups` with the manager idle.
- Then roll out to remaining hosts via the deploy action.

## Status: DONE — deployed to all servers (2026-06-28, module v1.10)
- Endpoints `/server_backup/agent/{presign,finalize}` live (token-auth, bucket-
  scoped). Agent reaches the manager by **IP + Host header** (no DNS dependency):
  `server.backup.agent_manager_url = http://46.101.127.229`, Host `exp.odex.sa`.
- Agent installed on BOTH servers; validated end-to-end (`1/1 uploaded` on ERP
  remote-PG and Odex local-PG). Crons: ERP 02:14, Odex 02:07 (jitter).
- **Auto-deploy**: hourly `cron_ensure_agents` installs the agent on every
  key-authorized host not yet enabled → new servers are covered automatically.
  Toggle: General Settings → "Auto-install Backup Agent on Every Server".
- Manager's own daily cron skips agent-enabled hosts (no double backup); it also
  refreshes each host's DB list right before backing up (for non-agent hosts).
- Fixed: `_ensure_agent_token` now reads via sudo (was regenerating the token
  each deploy because the field is admin-only → desynced agent config).
- Only secret on a server = the per-host agent token (least privilege). The
  Spaces key stays encrypted on the manager; the agent only holds a throwaway
  pre-signed URL during the run.

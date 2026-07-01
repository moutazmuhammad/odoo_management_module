# Self-Backup Agent — Fix & Monitoring Plan

Branch: `server-mgmt-backups-security`

## Diagnosis (confirmed on live server 46.101.127.229 / db `mgmt`, 2026-07-01)

- Both managed hosts (Odex Dev 138.197.188.136, ERP Dev 134.209.166.14) have the
  agent installed and cron firing nightly (02:07), but **every run fails**:

  ```
  urllib.error.HTTPError: HTTP Error 405: METHOD NOT ALLOWED
  ```

- Root cause: `manager_url = http://46.101.127.229` (Host: exp.odex.sa). nginx
  answers with **301 → https://exp.odex.sa/...**. Python `urllib` follows a 301 by
  **downgrading POST→GET**, and the POST-only JSON routes reject the GET (405/400).
- `last_backup` in Odoo only advanced on **manual** manager-side runs (SSH path,
  which never uses these HTTP endpoints). The nightly agent path never worked.
- Disk is fine (217 GB free on host 1). Pure redirect bug.
- Verified fix: a direct `POST https://exp.odex.sa/server_backup/agent/dblist`
  with the host token returns `200` + full DB list.

## Changes

1. **Agent redirect fix** — `ansible/playbooks/files/backup_agent.py`
   `post()` uses an opener with a redirect handler that **keeps POST + body**
   across 301/302/303/307/308, and an HTTPS handler carrying the ssl context so
   `insecure` still applies after an http→https redirect.

2. **Propagate fix to already-enrolled hosts** — `models/server_host.py` +
   `deploy_agent.yml`
   - `_AGENT_VERSION` bumped; new host field `agent_version`.
   - `action_deploy_agent` stamps the version; writes it into the on-server conf.
   - `_cron_ensure_agents` now also **redeploys** hosts whose `agent_version` is
     stale (not just not-yet-enrolled ones), so bug fixes reach live servers.

3. **Backup-existence monitor** — new daily cron `_cron_verify_backups`
   - For each key-authorized host with expected DBs, check the shared Space for
     an object under the host prefix modified in the last 48h (today or the day
     before). Configurable via `server.backup.max_age_hours` (default 48).
   - If missing: SSH-probe the host (free disk + agent log tail + agent/cron
     presence), set host `backup_review_needed` + `backup_review_reason`, and set
     `needs_review` on the host's instances (stages).
   - If present again: clear the host flag and the instance `needs_review` flags
     it set, so recovery auto-resolves.
   - Storage helper `_has_recent_object(prefix, max_age_hours)`.
   - Probe playbook `ansible/playbooks/backup_probe.yml`.

4. **UI** — `views/server_host_views.xml`
   Show `backup_review_needed` (alert), `backup_review_reason`, `last_backup_check`
   in the Backups group. Instances already show `needs_review`.

5. **Large backups** — verify the existing streaming-multipart path end-to-end
   (bounded-disk `_StreamingMultipart`, single<4GiB / multipart≥4GiB). No redesign.

## Test

- Copy module to manager, upgrade, redeploy agents (version bump forces it).
- Run agent manually on host 1 → confirm objects land in the bucket for today.
- Run `_cron_verify_backups` → confirm green; force a gap → confirm flag + reason.
- Push to GitHub.

import os
import re
import json
import math
import time
import base64
import logging

import psycopg2
from psycopg2 import errorcodes

from odoo import models, fields, api, _
from odoo.exceptions import UserError, AccessError, ValidationError

from .stage import GROUP_DEVOPS, GROUP_ADMIN

_logger = logging.getLogger(__name__)

# IPv4 or a hostname that must start with an alphanumeric (never a dash — a
# leading '-' could be interpreted by ssh as an option = argument injection).
IP_RE = re.compile(r'^(\d{1,3}\.){3}\d{1,3}$|^[A-Za-z0-9][A-Za-z0-9.\-]*$')
DISCOVERY_MARKER = 'ODOO_DISCOVERY_JSON:'


class ServerHost(models.Model):
    """A physical server reached over SSH with the single global key.

    The operator enters only the IP; everything else (services, versions,
    paths, ports) is auto-detected by :meth:`action_discover` and materialised
    as one ``server.stage`` per detected Odoo service.
    """
    _name = 'server.host'
    _description = 'Server Host'
    _order = 'name'

    name = fields.Char(string='Name', required=True)
    ip = fields.Char(string='IP Address', required=True)
    # Only the connection endpoint lives on the host: IP + port. The SSH user
    # and key are global (Settings → SSH).
    ssh_port = fields.Integer(
        string='Port', required=True, groups=GROUP_DEVOPS,
        default=lambda s: s.env['server.stage']._default_ssh_port(),
    )
    notes = fields.Text(string='Notes')
    # When set, this server's instances are hidden from plain Developers
    # (enforced by record rules); servers are DevOps/Admin-only regardless.
    devops_only = fields.Boolean(
        string='DevOps Only', groups=GROUP_DEVOPS, default=False,
        help="Hide this server's instances from plain Developers (Servers "
             "themselves are already DevOps/Administrator only).")
    # Admin-only: enable the daily auto-stop job for this server. Off by default.
    auto_stop_enabled = fields.Boolean(
        string='Stop Instances', groups=GROUP_DEVOPS, default=False,
        help="Auto-stop instances on this server whose service has been running "
             "longer than the configured number of days (Settings → Auto-Stop).",
    )

    key_authorized = fields.Boolean(string='Key Authorized', default=False, readonly=True)
    last_discovery = fields.Datetime(string='Last Discovery', readonly=True)
    # Durable result of the last background discovery (runs in a worker thread so a
    # slow host never times out the request). The live toast is pushed over the bus.
    op_label = fields.Char(string="Last Operation", readonly=True, copy=False)
    op_state = fields.Selection(
        [('running', '⏳ Running'), ('done', '✅ Success'), ('failed', '❌ Failed')],
        string="Last Operation Result", readonly=True, copy=False)
    op_time = fields.Datetime(string="Last Operation At", readonly=True, copy=False)
    op_detail = fields.Text(string="Last Operation Details", readonly=True, copy=False)
    # All servers back up to the single shared Space (configured globally in
    # General Settings). This selection becomes the top-level folder in the
    # bucket, so backups land under
    # <bucket>/<category>/<server>/<ip-or-domain>/<db>/.
    backup_category = fields.Selection(
        [('odex', 'Odex'), ('erp', 'ERP')],
        string='Backup Category', required=True, default='odex',
        help="Top-level folder for this server's backups in the shared Space: "
             "<bucket>/<category>/<server>/<ip-or-domain>/<db>/<db>_<date>.zip "
             "(e.g. erp/epr-dev-servers/46.101.127.229/mydb/mydb_2026-06-28.zip).")
    backup_extra_dbs = fields.Text(
        string='Additional Databases to Back Up', groups=GROUP_DEVOPS,
        help="Exact database names (comma- or newline-separated) to ALWAYS back "
             "up for this server, in addition to the one canonical DB auto-"
             "detected per stage. Use this for multi-database instances and for "
             "stages where several DB copies exist so the live one can't be "
             "guessed. Names not found on the server are ignored.")
    # Decentralized agent: when enabled, a local cron on the server runs the
    # backup (presign-on-demand) and the manager's daily cron skips this host.
    backup_agent_enabled = fields.Boolean(
        string='Self-Backup Agent Installed', readonly=True, copy=False,
        groups=GROUP_DEVOPS)
    agent_token = fields.Char(string='Agent Token', groups=GROUP_DEVOPS,
                              readonly=True, copy=False)
    # Version of the deployed agent code. The ensure-agents cron redeploys any
    # host whose stamp is older than _AGENT_VERSION so bug fixes reach live hosts.
    agent_version = fields.Char(string='Agent Version', groups=GROUP_DEVOPS,
                                readonly=True, copy=False)
    last_backup = fields.Datetime(string='Last Daily Backup', readonly=True)
    # Set by the daily backup-existence monitor when no backup for this host has
    # landed in the shared Space within the allowed window (today or the day
    # before). The reason captures free disk + the agent log tail so an operator
    # can see whether it is a space problem or a real failure.
    backup_review_needed = fields.Boolean(
        string='Backup Needs Review', readonly=True, copy=False,
        help="No recent backup was found in the bucket for this server. See the "
             "reason below (disk space / agent error).")
    backup_review_reason = fields.Text(
        string='Backup Review Reason', readonly=True, copy=False)
    last_backup_check = fields.Datetime(
        string='Last Backup Check', readonly=True, copy=False)
    stage_ids = fields.One2many('server.stage', 'host_id', string='Detected Instances')
    instance_count = fields.Integer(compute='_compute_instance_count')

    _sql_constraints = [
        ('unique_host_ip', 'unique(ip)', 'A host with this IP already exists!'),
    ]

    def _register_hook(self):
        """On every (re)start of the Odoo service, any operation still marked
        'running' belonged to a background worker thread that the restart killed —
        so it can never finish on its own. Mark such ops as failed/interrupted so
        the UI never sticks on a phantom 'Running' (a fresh run overwrites this)."""
        res = super()._register_hook()
        try:
            for model in ('server.host', 'server.stage'):
                stuck = self.env[model].sudo().search([('op_state', '=', 'running')])
                if stuck:
                    stuck.write({
                        'op_state': 'failed',
                        'op_time': fields.Datetime.now(),
                        'op_detail': _('Interrupted — the Odoo service was restarted '
                                       'while this was running. Please run it again.')})
            self.env.cr.commit()
        except Exception:  # noqa: BLE001 — never block startup
            self.env.cr.rollback()
            _logger.exception("Resetting stuck op_state on startup failed")
        return res

    @api.depends('stage_ids')
    def _compute_instance_count(self):
        for host in self:
            host.instance_count = len(host.stage_ids)

    @api.constrains('ip')
    def _check_ip(self):
        for host in self:
            if host.ip and not IP_RE.match(host.ip.strip()):
                raise ValidationError(_("Invalid IP address or hostname: %s") % host.ip)

    # ------------------------------------------------------------------
    # Connection helpers (key-only, reuse the stage runner)
    # ------------------------------------------------------------------
    def _build_inventory(self):
        """Inventory for this host using the global SSH user + key."""
        self.ensure_one()
        Stage = self.env['server.stage']
        key_file = Stage._ssh_key_file()
        ssh_user = Stage._default_ssh_user()
        inv = (
            f"myhost ansible_host={self.ip} "
            f"ansible_user={ssh_user} "
            f"ansible_port={self.ssh_port} "
        )
        if key_file:
            inv += f"ansible_ssh_private_key_file={key_file} "
        kh = self.env['server.stage']._known_hosts_file()
        inv += f"ansible_ssh_common_args='-o StrictHostKeyChecking=accept-new -o UserKnownHostsFile={kh}' "
        return inv

    def _run(self, playbook_name, extra_vars=None, timeout=None):
        playbook = os.path.join(
            os.path.dirname(__file__), '../ansible/playbooks', playbook_name
        )
        inventory = self._build_inventory()
        return self.env['server.stage']._run_ansible_playbook(
            playbook, inventory, extra_vars, timeout=timeout)

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------
    def _require_key(self):
        if not self.env['server.stage']._ssh_key_file():
            raise UserError(_(
                "No SSH key is configured. Paste the global private key in "
                "Server Management → GitHub Configuration (SSH section) first."
            ))

    def action_open_terminal(self):
        """Open the web SSH console for this host (new tab). DevOps+ allowed."""
        self.env['server.stage']._check_access(GROUP_DEVOPS)
        self.ensure_one()
        self._require_key()
        return {
            'type': 'ir.actions.act_url',
            'target': 'new',
            'url': '/server/terminal/%d' % self.id,
        }

    def action_test_connection(self):
        self.env['server.stage']._check_access(GROUP_DEVOPS)
        self.ensure_one()
        self._require_key()
        result = self._run('ping.yml')
        if result['success']:
            self.key_authorized = True
            return self.env['server.stage']._notify(_('✅ Connection successful'))
        raise UserError(_('❌ Connection failed: %s') % result['output'])

    def _refresh_databases(self):
        """Refresh the cached database list for every instance on this host in a
        single SSH session (one bulk psql lookup), and store it per stage."""
        self.ensure_one()
        stages = self.stage_ids.filtered('conf_file')
        if not stages:
            return
        spec = [[st.id, st.conf_file, st.odoo_user or ''] for st in stages]
        payload = base64.b64encode(json.dumps(spec).encode()).decode()
        inventory = self._build_inventory()
        playbook = os.path.join(
            os.path.dirname(__file__), '../ansible/playbooks/list_databases.yml')
        result = self.env['server.stage']._run_ansible_playbook(
            playbook, inventory, {'spec': payload})
        if not result.get('success'):
            return
        m = re.search(r'ODOO_DBLIST_JSON:([A-Za-z0-9+/=]+)', result.get('output') or '')
        if not m:
            return
        try:
            dbmap = json.loads(base64.b64decode(m.group(1)).decode())
        except Exception:
            _logger.warning("Failed to parse database map for host %s", self.id)
            return
        now = fields.Datetime.now()
        for st in stages.sudo():          # sudo: operators can discover but lack stage write
            dbs = dbmap.get(str(st.id)) or []
            st.available_databases = "\n".join(dbs)
            st.databases_updated = now

    def _refresh_and_commit(self, method, label, attempts=5):
        """Run a per-host refresh (slow SSH probe + ORM writes) then commit,
        retrying on a Postgres serialization failure / deadlock.

        The status (5 min) and DB-list (15 min) crons both keep their transaction
        snapshot open across a multi-second SSH probe and then write the SAME
        server_stage rows. When they overlap, one commit fails with
        `could not serialize access due to concurrent update` (SQLSTATE 40001) and
        the whole host's refresh was previously LOST. We roll back and retry with a
        fresh snapshot (re-probing), so the update lands instead of erroring out."""
        self.ensure_one()
        for attempt in range(attempts):
            try:
                getattr(self, method)()
                self.env.cr.commit()
                return True
            except psycopg2.OperationalError as e:
                self.env.cr.rollback()
                self.env.clear()  # drop stale cache so the retry re-reads/re-writes
                retryable = getattr(e, 'pgcode', None) in (
                    errorcodes.SERIALIZATION_FAILURE, errorcodes.DEADLOCK_DETECTED)
                if retryable and attempt < attempts - 1:
                    _logger.info("%s: serialization conflict on host %s — retry %s/%s",
                                 label, self.id, attempt + 1, attempts)
                    time.sleep(0.3 * (attempt + 1))
                    continue
                _logger.exception("%s failed for host %s", label, self.id)
                return False
            except Exception:  # noqa: BLE001
                self.env.cr.rollback()
                self.env.clear()
                _logger.exception("%s failed for host %s", label, self.id)
                return False

    @api.model
    def _cron_refresh_databases(self):
        """Background job (every 15 min): refresh cached DB lists for all hosts.
        Commits per host (with serialization retry) so one unreachable or
        conflicting server does not lose the others."""
        for host in self.search([]):
            host._refresh_and_commit('_refresh_databases', "Scheduled DB refresh")

    def _refresh_commits(self, stages=None):
        """Refresh the current HEAD commit recorded for every repo checkout on this
        host (one SSH session), and store it on each server.stage.repo.branch.path.

        `stages` limits the refresh to those instances (used by the per-stage
        "Get Commit" button); default is every instance on the host (cron)."""
        self.ensure_one()
        # sudo: a plain Server-Management user may press "Get Commit", and reading
        # paths / the instance odoo_user (a devops-only field) must not be blocked.
        stages = (stages if stages is not None else self.stage_ids).sudo()
        links = stages.mapped('repo_branch_paths')
        if not links:
            return
        # One spec entry per distinct path (a repo shared by instances of the same
        # owner is read once); carry the instance's odoo_user for sudo de-escalation.
        specs, seen = [], set()
        for link in links:
            path = (link.pull_path or '').strip()
            if not path or path in seen:
                continue
            seen.add(path)
            specs.append({'path': path, 'user': link.stage_id.odoo_user or ''})
        if not specs:
            return
        payload = base64.b64encode(json.dumps({'repos': specs}).encode()).decode()
        playbook = os.path.join(os.path.dirname(__file__),
                                '../ansible/playbooks/refresh_commits.yml')
        result = self.env['server.stage']._run_ansible_playbook(
            playbook, self._build_inventory(), {'spec': payload})
        if not result.get('success'):
            return
        m = re.search(r'ODOO_GITCOMMITS_JSON:([A-Za-z0-9+/=]+)', result.get('output') or '')
        if not m:
            return
        try:
            cmap = json.loads(base64.b64decode(m.group(1)).decode())
        except Exception:
            _logger.warning("Failed to parse commit map for host %s", self.id)
            return
        now = fields.Datetime.now()
        for link in links.sudo():
            info = cmap.get((link.pull_path or '').strip())
            if not info:
                continue
            link.write({
                'current_commit': info.get('commit') or '',
                'current_commit_short': info.get('commit_short') or '',
                'commit_subject': info.get('subject') or '',
                'commit_author': info.get('author') or '',
                'commit_date': info.get('date') or '',
                'commit_checked': now,
            })

    @api.model
    def _cron_refresh_commits(self):
        """Daily job: refresh the current git commit of every checkout on all hosts.
        Commits per host (with serialization retry) for resilience."""
        for host in self.search([]):
            host._refresh_and_commit('_refresh_commits', "Commit refresh")

    def _auto_stop(self, days):
        """Stop instances on this host (that opt in) whose service has been up
        longer than `days`. Returns the list of stopped service names."""
        self.ensure_one()
        stages = self.stage_ids.filtered(lambda s: s.auto_stop and s.service_name)
        if not stages or days <= 0:
            return []
        spec = {'days': days, 'services': stages.mapped('service_name')}
        payload = base64.b64encode(json.dumps(spec).encode()).decode()
        result = self._run('auto_stop.yml', {'spec': payload})
        if not result.get('success'):
            return []
        m = re.search(r'ODOO_AUTOSTOP_JSON:([A-Za-z0-9+/=]+)', result.get('output') or '')
        if not m:
            return []
        try:
            stopped = json.loads(base64.b64decode(m.group(1)).decode()) or []
        except Exception:
            return []
        for st in stages.sudo():
            if st.service_name in stopped:
                st.service_status = False
        return stopped

    @api.model
    def _cron_auto_stop(self):
        """Daily job: auto-stop stale instances on hosts that enabled it."""
        days = int(self.env['ir.config_parameter'].sudo().get_param(
            'server.autostop.days') or 0)
        if days <= 0:
            return
        for host in self.search([('auto_stop_enabled', '=', True)]):
            try:
                stopped = host._auto_stop(days)
                if stopped:
                    _logger.info("Auto-stopped on host %s: %s", host.name, stopped)
                self.env.cr.commit()
            except Exception:
                self.env.cr.rollback()
                _logger.exception("Auto-stop failed for host %s", host.id)

    def _service_statuses(self, stages=None):
        """Real systemd status of the given stages' services in one SSH session.
        Returns {service_name: 'active'|'inactive'|'failed'|...}."""
        self.ensure_one()
        stages = (stages or self.stage_ids).filtered('service_name')
        if not stages:
            return {}
        spec = base64.b64encode(
            json.dumps(stages.mapped('service_name')).encode()).decode()
        res = self._run('service_status.yml', {'spec': spec})
        return self._parse_backup_json(res.get('output'), 'ODOO_STATUS_JSON:') or {}

    def _refresh_status(self, stages=None):
        """Write each stage's odoo_status from the REAL systemd status (SSH)."""
        self.ensure_one()
        stages = (stages or self.stage_ids).filtered('service_name')
        if not stages:
            return
        statuses = self._service_statuses(stages)
        now = fields.Datetime.now()
        for st in stages.sudo():
            v = statuses.get(st.service_name)
            if v in ('active', 'activating'):
                st.odoo_status = 'running'
            elif v and v != 'unknown':
                # inactive / failed / deactivating -> genuinely stopped.
                st.odoo_status = 'stopped'
            elif v == 'unknown':
                # The probe itself could not determine the state (e.g. it errored
                # on the host). Do NOT assert 'stopped' — that made a running
                # service look down. Leave it 'unknown' instead.
                st.odoo_status = 'unknown'
            st.last_status_check = now

    # ------------------------------------------------------------------
    # Per-project daily backups (object storage, pre-signed uploads)
    # ------------------------------------------------------------------
    @staticmethod
    def _slug(text):
        """Slugify a name for an object key: lowercase, runs of non-alphanumerics
        collapse to a single dash. "Dev Server" -> "dev-server"."""
        s = re.sub(r'[^a-z0-9]+', '-', (text or '').strip().lower())
        return s.strip('-') or 'server'

    def _parse_backup_json(self, output, marker):
        m = re.search(marker + r'([A-Za-z0-9+/=]+)', output or '')
        if not m:
            return None
        try:
            return json.loads(base64.b64decode(m.group(1)).decode())
        except Exception:  # noqa: BLE001
            _logger.warning("Could not parse %s payload for host %s", marker, self.id)
            return None

    # Objects under ~5 GB use a single pre-signed PUT; larger ones use multipart
    # with pre-signed part URLs (512 MiB parts) so any size works, streamed.
    _BACKUP_SINGLE_LIMIT = 4 * 1024 ** 3
    _BACKUP_PART_SIZE = 512 * 1024 ** 2
    # Bump whenever the deployed agent code (files/backup_agent.py or
    # files/smart_backup.py) changes so the ensure-agents cron redeploys live
    # hosts. v2: agent survives the manager's http->https 301 (kept POST body).
    # v3: smart_backup.py made Python 3.6-compatible (dropped 3.7+ subprocess
    # capture_output=/text= kwargs) so detection/backup work on older hosts.
    _AGENT_VERSION = '3'

    @staticmethod
    def _backup_norm(value):
        """Normalize the SERVER-NAME path segment: lowercased, with spaces, dots and
        any other unsafe char turned into '-'. Underscores and hyphens are kept; runs
        of '-' collapse and are trimmed. (The server name never keeps dots — only the
        ip/domain segment and the '.zip' extension do.)"""
        s = re.sub(r'[^a-z0-9_-]+', '-', (value or '').strip().lower())
        return re.sub(r'-{2,}', '-', s).strip('-')

    @staticmethod
    def _backup_host_seg(value):
        """Normalize an IP/DOMAIN path segment — like _backup_norm but KEEPS dots
        (so keys read .../erp.example.com/... and .../46.101.127.229/...). Lowercased,
        spaces and other unsafe chars become '-', repeated '.'/'-' collapse, and edge
        '.'/'-' are stripped (guards against '..' path traversal)."""
        s = re.sub(r'[^a-z0-9._-]+', '-', (value or '').strip().lower())
        s = re.sub(r'\.{2,}', '.', re.sub(r'-{2,}', '-', s))
        return s.strip('.-')

    def _backup_server_seg(self):
        """Path segment identifying THIS server inside a backup key — a slug of the
        server's name (so keys read <category>/<server>/<domain>/<db>/...). Falls
        back to the IP if the name is empty."""
        self.ensure_one()
        return self._backup_norm(self.name) or self._backup_host_seg(self.ip)

    def _instance_seg(self, it):
        """The per-instance <domain-or-ip:port> path segment for a detected db item
        (from smart_backup). Uses the nginx domain when present, else this host's IP
        with the public port (nginx listen port, else the conf http_port). The IP is
        the manager-known host.ip; ':' becomes '-' (e.g. 46.101.127.229-8069)."""
        self.ensure_one()
        dom = (it.get('domain') or '').strip()
        if dom:
            return self._backup_host_seg(dom)
        port = str(it.get('port') or it.get('http_port') or '').strip()
        base = '%s:%s' % (self.ip, port) if port else (self.ip or '')
        return self._backup_host_seg(base)

    def _backup_targets(self, only_dbs=None):
        """Authoritative list of backup targets for this host: EVERY database of EVERY
        stage (from the cached available_databases, kept fresh by the 15-min
        _cron_refresh_databases), each with its path segment derived from the stage
        name (nginx domain, else ip:port). This is the manager's source of truth —
        the agent fetches the same list — so nothing is ever silently skipped."""
        self.ensure_one()
        targets, seen = [], set()
        for st in self.stage_ids:
            name = (st.name or '').strip()
            if ':' in name:                       # "<ip>:<port>" form
                domain, port = '', name.rsplit(':', 1)[1]
            else:                                 # nginx domain (or fallback name)
                domain, port = name, ''
            for db in (st.available_databases or '').splitlines():
                db = db.strip()
                if db and db not in seen:
                    seen.add(db)
                    targets.append({'db': db, 'domain': domain, 'port': port})
        for db in re.split(r'[,\n]', self.backup_extra_dbs or ''):  # additive override
            db = db.strip()
            if db and db not in seen:
                seen.add(db)
                targets.append({'db': db, 'domain': '', 'port': ''})
        if only_dbs:
            wanted = set(only_dbs)
            targets = [t for t in targets if t['db'] in wanted]
        return targets

    def _run_daily_backup(self, project=None, only_dbs=None):
        """Back up EVERY database of EVERY stage on this host to the shared Space
        (single or multipart, pre-signed — keys never leave Odoo), then prune old
        objects. Returns (uploaded, total, failed_dbs).

        Targets come from the manager's authoritative list (_backup_targets); the
        client only sizes + dumps + uploads them. Everything (dump + zip + upload)
        runs ON this client server; the manager only mints the pre-signed URLs."""
        self.ensure_one()
        Storage = self.env['server.backup.storage']
        if not Storage._keys_set():
            _logger.warning("Backup skipped for host %s: storage not configured.",
                            self.name)
            return (0, 0, [])

        targets = self._backup_targets(only_dbs=only_dbs)
        if not targets:
            _logger.info("No databases to back up on host %s", self.name)
            return (0, 0, [])
        want = {t['db'] for t in targets}

        # 1. Ask the client to size each target (filestore + bytes); the manager keeps
        #    the WHAT + path segment, the client adds what only it knows.
        targets_b64 = base64.b64encode(json.dumps(targets).encode()).decode()
        detect = self._run('backup_detect.yml', {'targets_b64': targets_b64})
        if not detect.get('success'):
            _logger.warning("Backup sizing failed on host %s: %s",
                            self.name, (detect.get('output') or '')[:300])
            return (0, len(want), sorted(want))
        items = self._parse_backup_json(detect.get('output'), 'ODOO_BACKUP_DETECT:') or []

        # 2. Back up each DB in its OWN ansible run so one huge/slow DB never starves
        #    the rest and completed DBs persist even if a later one fails.
        category = self.backup_category or 'odex'
        day = fields.Date.to_string(fields.Date.context_today(self))
        ip_seg = self._backup_host_seg(self.ip)
        server_seg = self._backup_server_seg()
        ok, done = 0, set()
        for it in items:
            db = it.get('db')
            try:
                if self._backup_one_db(Storage, it, category, day, ip_seg, server_seg):
                    ok += 1
                    done.add(db)
            except Exception:
                _logger.exception("Backup errored for %s/%s", self.name, db)
        # Anything we couldn't size/reach or that failed to upload = not done.
        failed = sorted(want - done)

        # 3. Prune old daily objects for this server (legacy folders too).
        try:
            Storage._prune(Storage._object_key([category, server_seg]) + '/')
            Storage._prune(Storage._object_key([category, ip_seg]) + '/')
            Storage._prune(Storage._object_key([category, (self.ip or '').replace('.', '-')]) + '/')
        except Exception:
            _logger.exception("Backup prune failed for host %s", self.name)

        self.sudo().last_backup = fields.Datetime.now()
        _logger.info("Daily backup on host %s: %s/%s databases uploaded to %s",
                     self.name, ok, len(want), Storage._bucket())
        return (ok, len(want), failed)

    def _backup_one_db(self, Storage, it, category, day, ip_seg, server_seg):
        """Build + upload ONE database in its own ansible run (own timeout).
        Completes/aborts its multipart upload. Returns True on success."""
        self.ensure_one()
        db = it.get('db')
        if not db:
            return False
        # <category>/<server>/<domain-or-ip:port>/<db>/<db>_<date>.zip
        # segment from nginx (domain) else host.ip:port; db kept verbatim.
        seg = self._instance_seg(it) or ip_seg
        key = Storage._object_key(
            [category, server_seg, seg, db, '%s_%s.zip' % (db, day)])
        size = int(it.get('size') or 0)
        fs = it.get('filestore') or ''
        mp = None
        if size < self._BACKUP_SINGLE_LIMIT:
            target = {'mode': 'single', 'filestore': fs,
                      'url': Storage._presign_put(key, ttl=12 * 3600)}
        else:
            upload_id = Storage._create_multipart(key)
            nparts = min(10000, math.ceil(size / self._BACKUP_PART_SIZE) + 5)
            part_urls = [Storage._presign_part(key, upload_id, i + 1)
                         for i in range(nparts)]
            target = {'mode': 'multipart', 'filestore': fs, 'upload_id': upload_id,
                      'part_size': self._BACKUP_PART_SIZE, 'part_urls': part_urls}
            mp = {'key': key, 'upload_id': upload_id}

        payload = base64.b64encode(json.dumps({db: target}).encode()).decode()
        run = self._run('backup_run.yml', {'targets_b64': payload}, timeout=6 * 3600)
        results = self._parse_backup_json(run.get('output'), 'ODOO_BACKUP_RESULT:') or {}
        res = results.get(db)
        if isinstance(res, dict) and res.get('ok'):
            if res.get('mode') == 'multipart' and mp:
                try:
                    Storage._complete_multipart(mp['key'], mp['upload_id'],
                                                res.get('parts') or [])
                except Exception:
                    _logger.exception("Complete multipart failed %s/%s", self.name, db)
                    Storage._abort_multipart(mp['key'], mp['upload_id'])
                    return False
            return True
        if mp:
            Storage._abort_multipart(mp['key'], mp['upload_id'])
        _logger.warning("Backup failed for %s/%s: %s", self.name, db,
                        (res or {}).get('error') if isinstance(res, dict) else res)
        return False

    def action_run_backup_now(self):
        """Manually run the full backup for this server in the BACKGROUND (bypasses
        the once-per-day / night-hour guards).

        Dumping + uploading every database can take many minutes; running it inline
        risked an HTTP timeout / lost-connection error. So the click returns
        immediately and the work runs in a worker thread that pushes the result
        (count, or the error) to the user as a bus toast and persists it on the host
        (op_* fields)."""
        self.env['server.stage']._check_access(GROUP_DEVOPS)
        self.ensure_one()
        if not self.env['server.backup.storage']._keys_set():
            raise UserError(_(
                "Backup storage is not configured. Set the bucket and keys in "
                "Server Management → General Settings → Backups."))
        self.sudo().write({'op_label': _('Run backup'), 'op_state': 'running',
                           'op_time': fields.Datetime.now(), 'op_detail': False})
        rec_id, dbname, uid = self.id, self.env.cr.dbname, self.env.uid
        label = _('Run backup')
        host_name = self.name

        def _worker():
            import time as _time
            import odoo
            # Phase 1 — the heavy work (dump + upload of every DB), which can run for
            # many minutes. Roll back this long-lived transaction afterwards: its
            # snapshot is stale by the end, so committing bookkeeping through it races
            # the 5-min status cron and fails with "could not serialize access due to
            # concurrent update". The uploads are already in object storage (not
            # transactional), so only the bookkeeping is deferred to phase 2.
            ok, uploaded, total, failed, detail = False, 0, 0, [], ''
            try:
                with odoo.registry(dbname).cursor() as cr:
                    env = api.Environment(cr, uid, {})
                    host = env['server.host'].browse(rec_id).sudo()
                    if not host.exists():
                        return
                    try:
                        uploaded, total, failed = host._run_daily_backup()
                        ok = not failed          # success ONLY if nothing was missed
                    except Exception as exc:  # noqa: BLE001 — report, never crash
                        _logger.exception("Manual backup failed for host %s", rec_id)
                        detail = (str(exc) or repr(exc))[-2000:]
                        failed = ['*']
                    cr.rollback()
            except Exception as exc:  # noqa: BLE001
                _logger.exception("Manual backup thread crashed for host %s", rec_id)
                detail = (str(exc) or repr(exc))[-2000:]
                failed = ['*']

            if ok:
                message = _("✅ Backup complete: %(n)s/%(t)s database(s) uploaded "
                            "for %(s)s.") % {'n': uploaded, 't': total, 's': host_name}
            else:
                named = [d for d in failed if d != '*']
                message = _("⚠️ Backup: %(n)s/%(t)s uploaded for %(s)s — %(f)s "
                            "failed.") % {'n': uploaded, 't': total, 's': host_name,
                                          'f': len(failed)}
                if not detail:
                    detail = _("Uploaded %(n)s of %(t)s. Failed (%(f)s): %(d)s") % {
                        'n': uploaded, 't': total, 'f': len(named),
                        'd': ", ".join(named[:50]) or '(sizing/connection error)'}
            title = ('✅ %s' % label) if ok else ('⚠️ %s' % label)
            # Phase 2 — short, retryable bookkeeping (op_* + last_backup), resilient
            # to the status cron writing concurrently.
            for attempt in range(5):
                try:
                    with odoo.registry(dbname).cursor() as cr:
                        env = api.Environment(cr, uid, {})
                        host = env['server.host'].browse(rec_id).sudo()
                        if host.exists():
                            vals = {'op_state': 'done' if ok else 'failed',
                                    'op_time': fields.Datetime.now(),
                                    'op_detail': '' if ok else (detail or message)}
                            if uploaded:  # stamp even on partial success
                                vals['last_backup'] = fields.Datetime.now()
                            host.write(vals)
                        env['server.stage']._send_op_bus(
                            uid, ok, title, message, sticky=not ok)
                        cr.commit()
                    break
                except Exception:  # noqa: BLE001 — serialization/lock conflict, retry
                    _logger.warning("Persist backup result for host %s: attempt %s "
                                    "failed, retrying", rec_id, attempt + 1)
                    _time.sleep(0.5 * (attempt + 1))

        import threading
        threading.Thread(target=_worker, name='odoo-host-backup', daemon=True).start()
        return self.env['server.stage']._op_started_toast(label, reload=True)

    def action_deploy_agent(self):
        """Install the self-backup agent + a daily Linux cron on this server. The
        server then backs itself up (presign-on-demand) with NO secret stored
        locally — the manager identifies the agent by its source IP — and the
        manager's daily cron skips this host afterwards."""
        self.env['server.stage']._check_access(GROUP_DEVOPS)
        self.ensure_one()
        self._require_key()
        Storage = self.env['server.backup.storage']
        if not Storage._keys_set():
            raise UserError(_(
                "Configure backup storage first (General Settings → Backups)."))
        ICP = self.env['ir.config_parameter'].sudo()
        web_url = (ICP.get_param('web.base.url') or '').strip()
        # The URL the managed servers use to reach the manager. Defaults to
        # web.base.url, but can be overridden (e.g. an IP) when the manager's
        # domain isn't resolvable from the stage servers. We always send the
        # web.base.url host as a Host header so nginx routes to the right vhost.
        manager_url = (ICP.get_param('server.backup.agent_manager_url') or web_url).strip()
        if not manager_url:
            raise UserError(_(
                "Set the manager URL the servers should use: General Settings → "
                "Backups → Agent Manager URL (or configure web.base.url)."))
        from urllib.parse import urlparse
        host_header = urlparse(web_url).hostname or ''
        try:
            hour = int(ICP.get_param('server.backup.hour', default='2') or 2)
        except (TypeError, ValueError):
            hour = 2
        hour = max(0, min(23, hour))
        res = self._run('deploy_agent.yml', {
            'manager_url': manager_url,
            'host_header': host_header,
            'backup_hour': hour,
            'jitter': (self.id * 7) % 60,           # spread servers across the hour
            'extra_dbs': (self.backup_extra_dbs or '').replace('\n', ','),
            'agent_version': self._AGENT_VERSION,
        })
        if not res.get('success'):
            raise UserError(_("Agent deploy failed:\n%s") % (res.get('output') or ''))
        self.sudo().write({'backup_agent_enabled': True,
                           'agent_version': self._AGENT_VERSION})
        return self.env['server.stage']._notify(
            _("✅ Self-backup agent installed on %s (daily at %02d:%02d, server "
              "time). The manager will no longer back this host up itself.")
            % (self.name, hour, (self.id * 7) % 60))

    @api.model
    def _cron_ensure_agents(self):
        """Make sure every reachable server runs the self-backup agent. New hosts
        get it installed automatically once their SSH key is authorized — so
        'every server takes backup' needs no manual step. Controlled by the
        'Auto-install backup agent' setting (on by default)."""
        ICP = self.env['ir.config_parameter'].sudo()
        if ICP.get_param('server.backup.agent_auto_deploy', default='1') in ('0', 'false', 'False', ''):
            return
        if not self.env['server.backup.storage']._keys_set():
            return
        # Install on hosts that don't have the agent yet, AND redeploy hosts whose
        # deployed agent code is older than the current version (so fixes — e.g.
        # the http->https redirect fix — reach already-enrolled servers).
        hosts = self.search([
            ('key_authorized', '=', True),
            '|', ('backup_agent_enabled', '=', False),
                 ('agent_version', '!=', self._AGENT_VERSION)])
        for host in hosts:
            try:
                host.action_deploy_agent()
                self.env.cr.commit()
            except Exception:
                self.env.cr.rollback()
                _logger.exception("Auto-deploy agent failed for host %s", host.id)

    @api.model
    def _cron_daily_backups(self):
        """Ticks hourly but only runs during the configured night hour, and at
        most once per server per day. Commits per host so one failure does not
        lose the others. (Manual "Run Backup Now" bypasses this guard.)"""
        ICP = self.env['ir.config_parameter'].sudo()
        try:
            hour = int(ICP.get_param('server.backup.hour', default='2'))
        except (TypeError, ValueError):
            hour = 2
        hour = max(0, min(23, hour))
        now = fields.Datetime.now()      # server time (UTC)
        if now.hour != hour:
            return
        Storage = self.env['server.backup.storage']
        if not (Storage._daily_enabled() and Storage._keys_set()):
            return
        today = now.date()
        # Every host is enrolled automatically (no per-host setup). Hosts that run
        # their own local cron agent back themselves up — skip those here.
        hosts = self.search([('backup_agent_enabled', '=', False)])
        for host in hosts:
            # Already backed up today (e.g. a manual run, or a second tick)? Skip.
            if host.last_backup and host.last_backup.date() == today:
                continue
            try:
                # Refresh this host's database list right before backing it up, so
                # the run (and the per-stage view) reflect today's live databases.
                try:
                    host._refresh_databases()
                except Exception:
                    _logger.exception("Pre-backup DB refresh failed for host %s",
                                      host.id)
                host._run_daily_backup()
                self.env.cr.commit()
            except Exception:
                self.env.cr.rollback()
                _logger.exception("Daily backup failed for host %s", host.id)

    def _backup_prefixes(self):
        """Every bucket prefix a backup for THIS host could live under: the
        current <category>/<server-name>/ layout plus the legacy IP-based folders
        (dotted + old dashed), so a host that predates the server-name layout is
        still recognised as having a recent backup."""
        self.ensure_one()
        Storage = self.env['server.backup.storage']
        category = self.backup_category or 'odex'
        segs = [self._backup_server_seg(), self._backup_host_seg(self.ip),
                (self.ip or '').replace('.', '-')]
        prefixes, seen = [], set()
        for seg in segs:
            if seg and seg not in seen:
                seen.add(seg)
                prefixes.append(Storage._object_key([category, seg]) + '/')
        return prefixes

    def _diagnose_backup_gap(self):
        """SSH-probe this host to explain why no recent backup exists: free disk on
        the scratch/backup paths, whether the agent + cron are installed, and the
        tail of the agent log. Returns a human-readable reason string (never
        raises — a probe failure is itself part of the reason)."""
        self.ensure_one()
        try:
            res = self._run('backup_probe.yml', timeout=120)
        except Exception as exc:  # noqa: BLE001
            return _("Could not probe the server (SSH/ansible error): %s") % (
                str(exc)[:300])
        if not res.get('success'):
            return _("Server unreachable for probe (SSH failed):\n%s") % (
                (res.get('output') or '')[:500])
        info = self._parse_backup_json(res.get('output'), 'ODOO_BACKUPPROBE_JSON:')
        if not info:
            return _("Backup missing; server probe returned no data.")
        lines = []
        disk = info.get('disk') or {}
        low = [p for p, d in disk.items()
               if isinstance(d, dict) and (d.get('free_gb') or 0) < 5]
        disk_txt = ", ".join(
            "%s: %sGB free (%s%% used)" % (p, d.get('free_gb'), d.get('pct_used'))
            for p, d in disk.items() if isinstance(d, dict))
        if low:
            lines.append(_("⚠️ LOW DISK on %s — free up space (a backup needs room "
                           "for one ~512 MiB part at a time).") % ", ".join(low))
        if disk_txt:
            lines.append(_("Disk: %s") % disk_txt)
        if not info.get('agent_installed'):
            lines.append(_("The self-backup agent is not installed "
                           "(/opt/odoo-backup/agent.py missing)."))
        if not info.get('cron_installed'):
            lines.append(_("The daily cron is not installed "
                           "(/etc/cron.d/odoo-backup missing)."))
        tail = (info.get('log_tail') or '').strip()
        if tail:
            lines.append(_("Agent log tail:\n%s") % tail[-1200:])
        else:
            lines.append(_("No agent log yet (/var/log/odoo-backup.log) — the "
                           "nightly run may never have started."))
        return "\n".join(lines)

    @api.model
    def _cron_verify_backups(self):
        """Daily safety net: make sure EVERY server has a fresh backup object in the
        shared Space (today or the day before). If a host's most recent backup is
        older than the allowed window, flag the host (backup_review_needed + a
        reason that includes free disk + the agent log) and mark its instances as
        'Needs Review'. When a fresh backup reappears, the flags auto-clear.

        This catches silent failures the per-host cron can't see — e.g. the agent
        failing every night — because it checks the bucket itself, the one place a
        real backup must show up."""
        Storage = self.env['server.backup.storage']
        if not Storage._keys_set():
            return
        ICP = self.env['ir.config_parameter'].sudo()
        try:
            max_age = int(ICP.get_param('server.backup.max_age_hours', default='48'))
        except (TypeError, ValueError):
            max_age = 48
        max_age = max(24, max_age)          # never alert before a full day passes
        now = fields.Datetime.now()
        for host in self.search([('key_authorized', '=', True)]):
            try:
                # Nothing to expect if the host has no databases to back up.
                if not host._backup_targets():
                    if host.backup_review_needed:
                        host.write({'backup_review_needed': False,
                                    'backup_review_reason': False,
                                    'last_backup_check': now})
                    self.env.cr.commit()
                    continue
                recent = any(Storage._has_recent_object(p, max_age_hours=max_age)
                             for p in host._backup_prefixes())
                if recent:
                    vals = {'last_backup_check': now}
                    if host.backup_review_needed:
                        vals.update({'backup_review_needed': False,
                                     'backup_review_reason': False})
                        # Clear only the review flags this monitor raised.
                        host.stage_ids.sudo().filtered('needs_review').write(
                            {'needs_review': False})
                    host.write(vals)
                else:
                    reason = host._diagnose_backup_gap()
                    header = _("No backup found in the bucket for '%s' in the last "
                               "%sh (checked %s).") % (
                        host.name, max_age, ", ".join(host._backup_prefixes()))
                    host.write({'backup_review_needed': True,
                                'backup_review_reason': header + "\n\n" + reason,
                                'last_backup_check': now})
                    host.stage_ids.sudo().write({'needs_review': True})
                    _logger.warning("Backup monitor: host %s has NO recent backup",
                                    host.name)
                self.env.cr.commit()
            except Exception:
                self.env.cr.rollback()
                _logger.exception("Backup verify failed for host %s", host.id)

    def action_discover(self):
        """Detect every Odoo service on the host and sync stages — in the BACKGROUND.

        Discovery SSHes in and runs ansible (plus a git ls-remote per repo), which
        can take a while; running it inline risked an HTTP timeout / lost-connection
        error. So the click returns immediately and the work runs in a worker thread
        that commits its DB sync and pushes the result (counts, or the error) to the
        user as a bus toast. The outcome is also persisted on the host (op_* fields)."""
        self.env['server.stage']._check_access(GROUP_DEVOPS)
        self.ensure_one()
        self._require_key()
        self.sudo().write({'op_label': _('Discover server'), 'op_state': 'running',
                           'op_time': fields.Datetime.now(), 'op_detail': False})
        rec_id, dbname, uid = self.id, self.env.cr.dbname, self.env.uid
        label = _('Discover server')

        def _mark_failed(message, detail):
            import odoo
            try:
                with odoo.registry(dbname).cursor() as cr:
                    env = api.Environment(cr, uid, {})
                    host = env['server.host'].browse(rec_id)
                    if host.exists():
                        host.sudo().write({
                            'op_state': 'failed', 'op_time': fields.Datetime.now(),
                            'op_detail': (detail or message or '')[-2000:]})
                    env['server.stage']._send_op_bus(
                        uid, False, '❌ %s' % label, message, sticky=True)
                    cr.commit()
            except Exception:  # noqa: BLE001
                _logger.exception("discover failure notify failed")

        def _worker():
            import time as _time
            import odoo
            # Phase 1 — run discovery (ansible) ONCE and parse it. No DB writes here
            # (the cursor is rolled back), so a phase-2 retry never re-runs ansible.
            instances = None
            try:
                with odoo.registry(dbname).cursor() as cr:
                    env = api.Environment(cr, uid, {})
                    host = env['server.host'].browse(rec_id).sudo()
                    if not host.exists():
                        return
                    result = host._run('discover_server.yml')
                    safe = env['server.stage']._redact_log(result.get('output'))
                    parsed = (host._parse_discovery(result['output'])
                              if result.get('success') else [])
                    cr.rollback()
                if not result.get('success'):
                    return _mark_failed(_('Discovery failed.'), safe)
                if not parsed:
                    return _mark_failed(
                        _('No Odoo services were detected on this server.'), safe)
                instances = parsed
            except Exception as exc:  # noqa: BLE001
                _logger.exception("Discovery (phase 1) crashed for host %s", rec_id)
                return _mark_failed(_('Discovery error'), (str(exc) or repr(exc))[-2000:])

            # Phase 2 — persist the sync, RETRYING on a concurrent-update serialization
            # error (the 5-min status cron writes the same stage rows; REPEATABLE READ
            # then raises "could not serialize access due to concurrent update").
            for attempt in range(5):
                try:
                    with odoo.registry(dbname).cursor() as cr:
                        env = api.Environment(cr, uid, {})
                        host = env['server.host'].browse(rec_id).sudo()
                        if not host.exists():
                            return
                        c, u, r = host._sync_instances(instances)
                        host.last_discovery = fields.Datetime.now()
                        host.write({'op_state': 'done', 'op_time': fields.Datetime.now(),
                                    'op_detail': ''})
                        env['server.stage']._send_op_bus(
                            uid, True, '✅ %s' % label,
                            _('🔍 Discovery complete: %(c)s created, %(u)s updated, '
                              '%(r)s removed.') % {'c': c, 'u': u, 'r': r})
                        cr.commit()
                    break
                except Exception as exc:  # noqa: BLE001 — serialization/lock conflict
                    _logger.warning("Discovery sync attempt %s for host %s failed: %s",
                                    attempt + 1, rec_id, exc)
                    if attempt == 4:
                        return _mark_failed(_('Discovery error'),
                                            (str(exc) or repr(exc))[-2000:])
                    _time.sleep(0.5 * (attempt + 1))

            # Warm status + DB-list caches AFTER the sync is safely committed (its own
            # short transactions, best-effort — never block or fail the discovery).
            for fn_name in ('_refresh_status', '_refresh_databases'):
                try:
                    with odoo.registry(dbname).cursor() as cr:
                        env = api.Environment(cr, uid, {})
                        host = env['server.host'].browse(rec_id).sudo()
                        if host.exists():
                            getattr(host, fn_name)()
                            cr.commit()
                except Exception:  # noqa: BLE001
                    _logger.exception("post-discovery %s failed", fn_name)

        import threading
        threading.Thread(target=_worker, name='odoo-host-discover', daemon=True).start()
        return self.env['server.stage']._op_started_toast(label, reload=True)

    # ------------------------------------------------------------------
    # Discovery parsing
    # ------------------------------------------------------------------
    @api.model
    def _parse_discovery(self, output):
        """Extract the base64-wrapped JSON payload from the playbook output."""
        match = re.search(DISCOVERY_MARKER + r'([A-Za-z0-9+/=]+)', output or '')
        if not match:
            return []
        try:
            payload = base64.b64decode(match.group(1)).decode('utf-8')
            data = json.loads(payload)
            return data if isinstance(data, list) else []
        except Exception as exc:  # noqa: BLE001
            _logger.warning("Failed to parse discovery payload: %s", exc)
            return []

    @staticmethod
    def _build_review_reason(missing, domain_ambiguous, chosen_domain, candidates):
        """Human-readable explanation of why discovery flagged an instance for
        manual review (shown on the stage). Empty string when nothing is wrong."""
        reasons = []
        if missing:
            reasons.append(_(
                "Some values could not be auto-detected (conf file, log file, "
                "odoo user or odoo-bin) — fill them in before running actions."))
        if domain_ambiguous:
            others = [d for d in candidates if d and d != chosen_domain]
            reasons.append(_(
                "This instance's port is served by multiple nginx domains: %(all)s. "
                "Discovery kept \"%(chosen)s\" — confirm it is the correct one "
                "(edit the Stage Name if it should be another).") % {
                    'all': ", ".join(candidates),
                    'chosen': chosen_domain or _("(none)"),
                } + (_(" Other candidate(s): %s.") % ", ".join(others) if others else ""))
        return "\n".join(reasons)

    def _sync_instances(self, instances):
        """Create/update one server.stage per detected service, and prune stages
        for services that no longer exist on the server."""
        self.ensure_one()
        Stage = self.env['server.stage'].sudo()
        created = updated = 0
        seen_services = set()
        for inst in instances:
            service_name = (inst.get('service_name') or '').strip()
            if not service_name:
                continue
            odoo_bin = inst.get('odoo_bin') or ''
            python_bin = inst.get('python_bin') or ''
            upgrade_path = (f"{python_bin} {odoo_bin}".strip()) if odoo_bin else ''
            missing = not all([
                inst.get('conf_file'), inst.get('log_file'),
                inst.get('odoo_user'), odoo_bin,
            ])
            # Ambiguous domain: the instance's port is fronted by MORE THAN ONE
            # nginx server_name (e.g. an apex domain and a subdomain both proxy to
            # the same Odoo). Discovery picks one deterministically, but which is
            # "correct" is the operator's call — flag it for manual review with the
            # full candidate list rather than silently choosing.
            domain_candidates = [d for d in (inst.get('domain_candidates') or []) if d]
            domain_ambiguous = bool(inst.get('domain_ambiguous')) and len(domain_candidates) > 1
            # Stage name uses the SAME source as the backup path (nginx domain, else
            # <ip>:<port> where port = nginx listen port (domainless vhost) or the
            # conf http_port). Only difference vs the bucket path: the name keeps
            # ip:port while the path uses ip-port.
            domain = (inst.get('domain') or '').strip()
            pub_port = str(inst.get('pub_port') or inst.get('http_port') or '').strip()
            if domain:
                stage_name = domain
            elif pub_port:
                stage_name = f"{self.ip}:{pub_port}"
            else:
                stage_name = f"{self.name} / {service_name}"
            vals = {
                'host_id': self.id,
                'name': stage_name,
                'service_name': service_name,
                'odoo_version': inst.get('odoo_version') or '',
                'odoo_bin': odoo_bin,
                'python_bin': python_bin,
                'conf_file': inst.get('conf_file') or '',
                'nginx_file': inst.get('nginx_file') or '',
                'log_file_path': inst.get('log_file') or '',
                'upgrade_module_path': upgrade_path,
                'odoo_user': inst.get('odoo_user') or '',
                'http_port': int(inst['http_port']) if str(inst.get('http_port') or '').isdigit() else 0,
                'needs_review': bool(missing or domain_ambiguous),
                'review_reason': self._build_review_reason(
                    missing, domain_ambiguous, domain, domain_candidates),
            }
            # Auto-detected master password (plaintext from the conf). Only set it
            # when found, so a manual entry is never wiped by a later discovery.
            admin_pw = (inst.get('admin_passwd') or '').strip()
            if admin_pw:
                vals['admin_password'] = admin_pw
            vals['available_modules'] = "\n".join(inst.get('modules') or [])
            vals['available_odoo_modules'] = "\n".join(inst.get('odoo_modules') or [])
            existing = Stage.search([
                ('host_id', '=', self.id),
                ('service_name', '=', service_name),
            ], limit=1)
            if existing:
                # Preserve hand-edited fields: discovery must not overwrite any
                # field the user corrected by hand (tracked in overridden_fields),
                # e.g. a real domain the nginx file doesn't carry. from_discovery
                # tells stage.write() this is an automated sync, so it does NOT
                # re-mark the remaining fields as overrides.
                protected = set(filter(None, (existing.overridden_fields or '').split(',')))
                write_vals = {k: v for k, v in vals.items() if k not in protected}
                existing.with_context(from_discovery=True).write(write_vals)
                stage = existing
                updated += 1
            else:
                stage = Stage.with_context(from_discovery=True).create(vals)
                created += 1
            seen_services.add(service_name)
            self._sync_repos(stage, inst.get('repos') or [])

        # Prune: a service that is no longer on the server -> remove its stage
        # (and, via ondelete=cascade, its repo-path links). Only runs when the
        # discovery actually returned services, so a failed scan never wipes data.
        removed = 0
        if seen_services:
            stale = self.stage_ids.filtered(lambda s: s.service_name not in seen_services)
            removed = len(stale)
            if stale:
                _logger.info("Discovery on host %s pruned %s removed instance(s): %s",
                             self.name, removed, stale.mapped('service_name'))
                stale.sudo().unlink()
        return created, updated, removed

    def _sync_repos(self, stage, repos):
        """Create/update server.repository + branch + on-server path records for
        the git repos discovered under this instance's addons path."""
        Repo = self.env['server.repository'].sudo()
        Branch = self.env['server.repository.branch'].sudo()
        Path = self.env['server.stage.repo.branch.path'].sudo()
        for r in repos:
            # Strip any embedded credentials (user:token@) — never store secrets,
            # and keep the URL canonical so dedup + token-based pull work.
            url = re.sub(r'://[^/@]+@', '://', (r.get('url') or '').strip())
            path = (r.get('path') or '').strip()
            branch_name = (r.get('branch') or '').strip() or 'HEAD'
            if not url or not path:
                continue
            # Never offer the official Odoo source as a pull target.
            if re.search(r'github\.com[:/]odoo/', url):
                continue
            repo = Repo.search([('url', '=', url)], limit=1)
            if not repo:
                repo = Repo.create({'name': self._repo_name_from_url(url), 'url': url})
            # Register EVERY branch discovered on the remote (not just the checked-out
            # one) so the Pull wizard can offer them all. The current branch is always
            # included so the path link below resolves even if the listing was empty.
            all_names = list(dict.fromkeys(
                [branch_name] + [b.strip() for b in (r.get('branches') or []) if b.strip()]))
            existing = {b.name: b
                        for b in Branch.search([('repository_id', '=', repo.id)])}
            for bn in all_names:
                if bn and bn not in existing:
                    existing[bn] = Branch.create({'name': bn, 'repository_id': repo.id})
            branch = existing.get(branch_name) or Branch.create(
                {'name': branch_name, 'repository_id': repo.id})
            commit_vals = {
                'current_commit': (r.get('commit') or '').strip(),
                'current_commit_short': (r.get('commit_short') or '').strip(),
                'commit_subject': (r.get('commit_subject') or '').strip(),
                'commit_author': (r.get('commit_author') or '').strip(),
                'commit_date': (r.get('commit_date') or '').strip(),
                'commit_checked': fields.Datetime.now(),
            }
            link = Path.search([
                ('stage_id', '=', stage.id), ('repository_id', '=', repo.id),
                ('pull_path', '=', path),
            ], limit=1)
            if link:
                link.write({'branch_id': branch.id, **commit_vals})
            else:
                Path.create({
                    'stage_id': stage.id, 'repository_id': repo.id,
                    'branch_id': branch.id, 'pull_path': path, **commit_vals,
                })

    @staticmethod
    def _repo_name_from_url(url):
        """Derive a readable repo name from a git URL (basename without .git)."""
        name = url.rstrip('/').split('/')[-1]
        if ':' in name and '/' not in name:  # scp-style git@host:org/repo.git tail
            name = name.split(':')[-1]
        if name.endswith('.git'):
            name = name[:-4]
        return name or url

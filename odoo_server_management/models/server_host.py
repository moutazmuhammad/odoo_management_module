import os
import re
import json
import base64
import logging

from odoo import models, fields, api, _
from odoo.exceptions import UserError, AccessError, ValidationError

from .stage import GROUP_OPERATOR, GROUP_ADMIN

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
        string='Port', required=True,
        default=lambda s: s.env['server.stage']._default_ssh_port(),
    )
    notes = fields.Text(string='Notes')
    # Admin-only: enable the daily auto-stop job for this server.
    auto_stop_enabled = fields.Boolean(
        string='Stop Instances', groups=GROUP_ADMIN,
        help="Auto-stop instances on this server whose service has been running "
             "longer than the configured number of days (Settings → Auto-Stop).",
    )

    key_authorized = fields.Boolean(string='Key Authorized', default=False, readonly=True)
    last_discovery = fields.Datetime(string='Last Discovery', readonly=True)
    stage_ids = fields.One2many('server.stage', 'host_id', string='Detected Instances')
    instance_count = fields.Integer(compute='_compute_instance_count')

    _sql_constraints = [
        ('unique_host_ip', 'unique(ip)', 'A host with this IP already exists!'),
    ]

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

    def _run(self, playbook_name, extra_vars=None):
        playbook = os.path.join(
            os.path.dirname(__file__), '../ansible/playbooks', playbook_name
        )
        inventory = self._build_inventory()
        return self.env['server.stage']._run_ansible_playbook(playbook, inventory, extra_vars)

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
        """Open the web SSH console for this host (new tab). Operator+ allowed."""
        self.env['server.stage']._check_access(GROUP_OPERATOR)
        self.ensure_one()
        self._require_key()
        return {
            'type': 'ir.actions.act_url',
            'target': 'new',
            'url': '/server/terminal/%d' % self.id,
        }

    def action_test_connection(self):
        self.env['server.stage']._check_access(GROUP_OPERATOR)
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

    @api.model
    def _cron_refresh_databases(self):
        """Background job (every 15 min): refresh cached DB lists for all hosts.
        Commits per host so one unreachable server does not lose the others."""
        for host in self.search([]):
            try:
                host._refresh_databases()
                self.env.cr.commit()
            except Exception:
                self.env.cr.rollback()
                _logger.exception("Scheduled DB refresh failed for host %s", host.id)

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

    def action_check_status(self):
        """Refresh the Odoo status of every instance on this host (parallel)."""
        self.env['server.stage']._check_access(GROUP_OPERATOR)
        self.ensure_one()
        self.stage_ids.action_check_status()
        return self.env['server.stage']._notify(
            _('🔄 Status refreshed for %s instance(s).') % len(self.stage_ids))

    def action_discover(self):
        """Detect every Odoo service on the host and sync stages."""
        self.env['server.stage']._check_access(GROUP_OPERATOR)
        self.ensure_one()
        self._require_key()
        result = self._run('discover_server.yml')
        # The raw output can embed the base64 discovery payload (which contains
        # the detected master password); redact it before showing it to a user.
        safe_output = self.env['server.stage']._redact_log(result['output'])
        if not result['success']:
            raise UserError(_('❌ Discovery failed: %s') % safe_output)

        instances = self._parse_discovery(result['output'])
        if not instances:
            raise UserError(_(
                "No Odoo services were detected on %s.\n\n%s"
            ) % (self.ip, safe_output))

        created, updated, removed = self._sync_instances(instances)
        self.last_discovery = fields.Datetime.now()
        # Populate live status (parallel, ~1s) and the DB-list cache (one SSH)
        # so the form and wizards are ready immediately, not after the next cron.
        try:
            self.stage_ids.action_check_status()
        except Exception:
            pass
        try:
            self._refresh_databases()
        except Exception:
            pass
        return self.env['server.stage']._notify(
            _('🔍 Discovery complete: %(c)s created, %(u)s updated, %(r)s removed.',
              c=created, u=updated, r=removed)
        )

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
            # Stage name = domain (from nginx) else ip:port else service name.
            http_port = str(inst.get('http_port') or '').strip()
            domain = (inst.get('domain') or '').strip()
            if domain:
                stage_name = domain
            elif http_port:
                stage_name = f"{self.ip}:{http_port}"
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
                'log_file_path': inst.get('log_file') or '',
                'upgrade_module_path': upgrade_path,
                'odoo_user': inst.get('odoo_user') or '',
                'http_port': int(inst['http_port']) if str(inst.get('http_port') or '').isdigit() else 0,
                'needs_review': missing,
            }
            # Auto-detected master password (plaintext from the conf). Only set it
            # when found, so a manual entry is never wiped by a later discovery.
            admin_pw = (inst.get('admin_passwd') or '').strip()
            if admin_pw:
                vals['admin_password'] = admin_pw
            vals['available_modules'] = "\n".join(inst.get('modules') or [])
            existing = Stage.search([
                ('host_id', '=', self.id),
                ('service_name', '=', service_name),
            ], limit=1)
            if existing:
                existing.write(vals)
                stage = existing
                updated += 1
            else:
                stage = Stage.create(vals)
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
            branch = Branch.search([
                ('repository_id', '=', repo.id), ('name', '=', branch_name),
            ], limit=1)
            if not branch:
                branch = Branch.create({'name': branch_name, 'repository_id': repo.id})
            link = Path.search([
                ('stage_id', '=', stage.id), ('repository_id', '=', repo.id),
                ('pull_path', '=', path),
            ], limit=1)
            if link:
                link.write({'branch_id': branch.id})
            else:
                Path.create({
                    'stage_id': stage.id, 'repository_id': repo.id,
                    'branch_id': branch.id, 'pull_path': path,
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

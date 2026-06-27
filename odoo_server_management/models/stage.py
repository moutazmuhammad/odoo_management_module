import os
import re
import json
import shutil
import logging
import tempfile
import subprocess
import warnings
import requests

from cryptography.fernet import Fernet, InvalidToken
from urllib3.exceptions import InsecureRequestWarning
from odoo import models, fields, api, _
from odoo.exceptions import UserError, AccessError

_logger = logging.getLogger(__name__)

# Env var that, if set, supplies the Fernet key (urlsafe base64, 32 bytes).
# Preferred for production so the key lives outside the DB *and* the filesystem.
SECRET_KEY_ENV = 'ODOO_SERVER_MGMT_KEY'

# Role groups used for in-method authorization (defense in depth behind sudo()).
# Hierarchy (each implies the previous): User -> Operator -> Administrator.
GROUP_USER = 'odoo_server_management.group_user'          # perform actions
GROUP_OPERATOR = 'odoo_server_management.group_operator'  # + manage/discover servers
GROUP_ADMIN = 'odoo_server_management.group_admin'        # + see all details + settings

# Keys whose values must never be written to ir.logging / process output.
SENSITIVE_VAR_KEYS = {
    'github_token', 'admin_password', 'ssh_password', 'master_pwd',
    'db_password', 'public_key', 'private_key',
}


class Stage(models.Model):
    _name = 'server.stage'
    _description = 'Server Stage'
    _order = 'name'

    # ===========================
    # Fields
    # ===========================
    name = fields.Char(string='Stage Name', required=True)
    client_stage = fields.Boolean(string='Client Server', default=False)
    notes = fields.Text(string='Notes')
    # Per-user flag for the UI: can the current user run operational actions on
    # this instance? Client servers require Operator+, others any User.
    can_act = fields.Boolean(compute='_compute_can_act', depends_context=('uid',))

    @api.depends('client_stage')
    def _compute_can_act(self):
        is_operator = (self.env.su
                       or self.env.user.has_group('odoo_server_management.group_operator'))
        for rec in self:
            rec.can_act = (not rec.client_stage) or is_operator

    host_id = fields.Many2one(
        'server.host',
        string='Server Host',
        required=True,
        ondelete='cascade',
        help="Physical server this Odoo instance runs on. All connection "
             "details (IP and port) live on the host; the SSH user and key are "
             "global (Settings).",
    )
    # Connection info is read from the host (host_id.ip / host_id.ssh_port) plus
    # the global SSH user/key — it is no longer duplicated on the stage.

    repo_branch_paths = fields.One2many(
        'server.stage.repo.branch.path',
        'stage_id',
        string='Repository Branch',
        readonly=True
    )

    # Per-instance Odoo details — auto-detected. One host may run several
    # services across several versions, so every path/version lives here.
    service_name = fields.Char(string='Service Name', required=True)
    odoo_version = fields.Char(string='Odoo Version', readonly=True)
    odoo_bin = fields.Char(string='odoo-bin Path', readonly=True, groups=GROUP_ADMIN)
    python_bin = fields.Char(string='Python Path', readonly=True, groups=GROUP_ADMIN)
    # Auto-detected — optional so partially-discovered instances can still be
    # saved and flagged via needs_review; actions validate before use.
    log_file_path = fields.Char(string='Log File Path', groups=GROUP_ADMIN)
    conf_file = fields.Char(string='Conf File', groups=GROUP_ADMIN)
    upgrade_module_path = fields.Char(string='Upgrade Module Path', groups=GROUP_ADMIN)
    http_port = fields.Integer(string='HTTP Port', readonly=True)
    needs_review = fields.Boolean(
        string='Needs Review', default=False,
        help="Set when auto-discovery could not determine every value.",
    )

    odoo_user = fields.Char(string='Odoo User', groups=GROUP_ADMIN)
    # Stored encrypted at rest; never exposed directly in views. The plaintext
    # is only available through the computed `admin_password` (DevOps only).
    admin_password_enc = fields.Char(string='Odoo Admin Password (encrypted)', groups=GROUP_ADMIN)
    admin_password = fields.Char(
        string='Odoo Admin Password', groups=GROUP_ADMIN,
        compute='_compute_admin_password', inverse='_inverse_admin_password',
        store=False,
    )

    @api.depends('admin_password_enc')
    def _compute_admin_password(self):
        Stage = self.env['server.stage']
        for rec in self:
            rec.admin_password = Stage._decrypt_secret(rec.admin_password_enc)

    def _inverse_admin_password(self):
        Stage = self.env['server.stage']
        for rec in self:
            rec.admin_password_enc = Stage._encrypt_secret(rec.admin_password)

    service_status = fields.Boolean(string='Service Status', default=False, readonly=True)
    # Admin-only: whether the daily auto-stop job may stop this instance (only
    # has effect when its host's "Stop Instances" is enabled).
    auto_stop = fields.Boolean(
        string='Auto-Stop', default=True, groups=GROUP_ADMIN,
        help="If the server's 'Stop Instances' is on, stop this instance once "
             "its service has been running longer than the configured days.",
    )
    # Stored (not computed-on-read): a live HTTP probe per render was the main
    # slowdown — opening a host re-probed every stage. It is now refreshed only
    # on demand via the "Check Status" button, and shown instantly everywhere.
    odoo_status = fields.Selection(
        [("running", "🟢 Running"), ("stopped", "🔴 Stopped"), ("unknown", "⚪ Not checked")],
        string="Odoo Status", default='unknown', readonly=True, copy=False,
    )
    last_status_check = fields.Datetime(string="Last Status Check", readonly=True)
    # Cached DB list (newline-separated), refreshed by a background cron every
    # 15 min so the backup/upgrade wizards open instantly without an SSH call.
    available_databases = fields.Text(string="Available Databases", readonly=True, copy=False)
    databases_updated = fields.Datetime(string="Databases Updated", readonly=True)
    # Cached list of the instance's custom modules (newline-separated), detected
    # during discovery — powers the Upgrade wizard's module dropdown.
    available_modules = fields.Text(string="Available Modules", readonly=True, copy=False)

    _sql_constraints = [
        ('unique_stage_name', 'unique(name)', 'Stage name must be unique!'),
    ]

    # ===========================
    # Defaults / config helpers
    # ===========================
    @api.model
    def _default_ssh_user(self):
        return self.env['ir.config_parameter'].sudo().get_param('server.ssh.user') or 'root'

    @api.model
    def _default_ssh_port(self):
        try:
            return int(self.env['ir.config_parameter'].sudo().get_param('server.ssh.port') or 22)
        except (TypeError, ValueError):
            return 22

    @api.model
    def _ssh_key_file(self):
        """Path to the single global SSH private key used for all servers.

        Prefers an explicit key file path; otherwise materializes the pasted
        private key to a 0600 file on the Odoo host. Access is key-only.
        """
        ICP = self.env['ir.config_parameter'].sudo()
        path = ICP.get_param('server.ssh.private_key_file')
        if path and os.path.exists(path):
            return path
        key_text = self._get_secret_param('server.ssh.private_key')
        if key_text:
            return self._materialize_key(key_text)
        return False

    @api.model
    def _validate_private_key(self, key_text):
        """Raise a clear error if `key_text` is not a structurally valid PEM/
        OpenSSH private key. Empty is allowed (means 'not configured').

        Catches the common failure where ssh later reports a cryptic
        'Load key ...: invalid format' — e.g. a single-line paste, a public key,
        or stray text — by checking it up front when the user saves."""
        text = (key_text or '').strip()
        if not text:
            return True
        lines = [ln for ln in text.splitlines() if ln.strip()]
        first, last = lines[0].strip(), lines[-1].strip()
        if (len(lines) < 3
                or not first.startswith('-----BEGIN ') or 'PRIVATE KEY' not in first
                or not last.startswith('-----END ') or not last.endswith('-----')):
            raise UserError(_(
                "The SSH private key is not in a valid format. Paste the full "
                "PEM/OpenSSH *private* key, including the\n"
                "  -----BEGIN ... PRIVATE KEY-----\n  ...multiple lines...\n"
                "  -----END ... PRIVATE KEY-----\n"
                "header and footer on their own lines (not a public key, and "
                "without a passphrase)."
            ))
        return True

    @api.model
    def _known_hosts_file(self):
        """Stable, module-managed known_hosts file (0600) for host-key pinning."""
        from odoo.tools import config
        base = config.get('data_dir') or tempfile.gettempdir()
        key_dir = os.path.join(base, 'server_mgmt_ssh')
        os.makedirs(key_dir, mode=0o700, exist_ok=True)
        kh = os.path.join(key_dir, 'known_hosts')
        if not os.path.exists(kh):
            os.close(os.open(kh, os.O_WRONLY | os.O_CREAT, 0o600))
        return kh

    @api.model
    def _materialize_key(self, key_text):
        """Write the pasted private key to a stable 0600 file, return its path."""
        from odoo.tools import config
        base = config.get('data_dir') or tempfile.gettempdir()
        key_dir = os.path.join(base, 'server_mgmt_ssh')
        os.makedirs(key_dir, mode=0o700, exist_ok=True)
        try:
            os.chmod(key_dir, 0o700)
        except OSError:
            pass
        key_path = os.path.join(key_dir, 'global_key')
        content = key_text.strip() + '\n'
        current = None
        if os.path.exists(key_path):
            try:
                with open(key_path) as fh:
                    current = fh.read()
            except OSError:
                pass
        if current != content:
            fd = os.open(key_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, 'w') as fh:
                fh.write(content)
            os.chmod(key_path, 0o600)
        return key_path

    # ===========================
    # Encryption at rest (Fernet)
    # ===========================
    # Marker prefixing every ciphertext we produce. Lets _decrypt_secret tell
    # our tokens apart from legacy plaintext values, so migration is painless
    # and self-healing (a legacy value is returned as-is, then re-encrypted on
    # the next save).
    _SECRET_PREFIX = 'enc$'

    @api.model
    def _secret_fernet(self):
        """Return a Fernet built from the module's encryption key.

        Key source order: env var (best — outside DB and disk), else a 0600
        key file under data_dir (auto-generated once). The key is NEVER stored
        in the database, so a DB dump alone cannot decrypt the secrets.
        """
        env_key = os.environ.get(SECRET_KEY_ENV)
        if env_key:
            return Fernet(env_key.encode() if isinstance(env_key, str) else env_key)

        from odoo.tools import config
        base = config.get('data_dir') or tempfile.gettempdir()
        key_dir = os.path.join(base, 'server_mgmt_ssh')
        os.makedirs(key_dir, mode=0o700, exist_ok=True)
        key_path = os.path.join(key_dir, 'secret.key')
        if os.path.exists(key_path):
            with open(key_path, 'rb') as fh:
                key = fh.read().strip()
        else:
            key = Fernet.generate_key()
            fd = os.open(key_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, 'wb') as fh:
                fh.write(key)
            os.chmod(key_path, 0o600)
        return Fernet(key)

    @api.model
    def _encrypt_secret(self, plaintext):
        """Encrypt a string for storage. Empty/false passes through unchanged."""
        if not plaintext:
            return plaintext
        if isinstance(plaintext, str) and plaintext.startswith(self._SECRET_PREFIX):
            return plaintext  # already encrypted
        token = self._secret_fernet().encrypt(str(plaintext).encode())
        return self._SECRET_PREFIX + token.decode()

    @api.model
    def _decrypt_secret(self, stored):
        """Decrypt a stored secret. Legacy plaintext (no marker) is returned
        unchanged so old values keep working until re-saved."""
        if not stored or not isinstance(stored, str):
            return stored
        if not stored.startswith(self._SECRET_PREFIX):
            return stored  # legacy plaintext
        token = stored[len(self._SECRET_PREFIX):].encode()
        try:
            return self._secret_fernet().decrypt(token).decode()
        except (InvalidToken, ValueError):
            _logger.error("Failed to decrypt a stored secret (wrong/rotated key?).")
            return ''

    @api.model
    def _get_secret_param(self, key, default=''):
        """Read and decrypt an encrypted ir.config_parameter."""
        raw = self.env['ir.config_parameter'].sudo().get_param(key, default='')
        return self._decrypt_secret(raw) or default

    @api.model
    def _set_secret_param(self, key, value):
        """Encrypt and store a secret ir.config_parameter."""
        self.env['ir.config_parameter'].sudo().set_param(
            key, self._encrypt_secret(value or '') or '')

    # ===========================
    # Authorization (defense in depth — methods run under sudo)
    # ===========================
    def _check_access(self, *groups):
        """Raise AccessError unless the *real* user belongs to one of groups."""
        if self.env.su:
            # Already running as superuser (e.g. cron / server action) — trust it.
            return
        user = self.env.user
        if not any(user.has_group(g) for g in groups):
            raise AccessError(_("You are not allowed to perform this operation."))

    def _check_action_access(self):
        """Gate an operational action: a **Client Server** instance may only be
        acted on by Operators/Admins; a normal instance by any User."""
        self.ensure_one()
        self._check_access(GROUP_OPERATOR if self.client_stage else GROUP_USER)

    # ===========================
    # Helpers
    # ===========================
    # Probe timeout per attempt (short — this is just a liveness check).
    _STATUS_TIMEOUT = 3

    @api.model
    def _probe_status(self, name):
        """Liveness probe for one instance by its `name` (domain or ip:port).

        Pure network call — touches no ORM, so it is safe to run from worker
        threads (see action_check_status). Returns 'running' or 'stopped'."""
        if not name:
            return 'stopped'
        base_url = re.sub(r'^(https?://)?', '', name.strip().lower()).rstrip('/')
        port = None
        if ':' in base_url:
            try:
                base_url, port_str = base_url.rsplit(':', 1)
                port = int(port_str)
                if not (1 <= port <= 65535):
                    return 'stopped'
            except ValueError:
                return 'stopped'

        headers = {"User-Agent": "Odoo-Server-Management"}
        suffix = f":{port}" if port else ""
        # Try https then http, one quick attempt each (verify=False avoids a slow
        # extra SSL-retry round). Total worst case ~2 × _STATUS_TIMEOUT.
        urls = [f"https://{base_url}{suffix}/web/login",
                f"http://{base_url}{suffix}/web/login"]
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", InsecureRequestWarning)
            for url in urls:
                try:
                    resp = requests.get(url, headers=headers,
                                        timeout=self._STATUS_TIMEOUT, verify=False)
                    if resp.status_code == 200:
                        return 'running'
                except Exception:
                    continue
        return 'stopped'

    def action_check_status(self):
        """Refresh odoo_status for the selected stage(s). Probes run in parallel
        so checking many instances (or many users acting at once) stays fast and
        never blocks on render."""
        self._check_access(GROUP_USER)
        self = self.sudo()
        names = {rec.id: rec.name for rec in self}
        results = {}
        if names:
            from concurrent.futures import ThreadPoolExecutor
            with ThreadPoolExecutor(max_workers=min(12, len(names))) as pool:
                futures = {pool.submit(self._probe_status, nm): rid
                           for rid, nm in names.items()}
                for fut in futures:
                    rid = futures[fut]
                    try:
                        results[rid] = fut.result()
                    except Exception:
                        results[rid] = 'stopped'
        now = fields.Datetime.now()
        for rec in self:
            rec.odoo_status = results.get(rec.id, 'unknown')
            rec.last_status_check = now
        return self._notify(_('🔄 Status refreshed for %s instance(s).') % len(self))

    @api.model
    def _cron_refresh_status(self):
        """Background job: refresh every instance's running/stopped status so the
        UI stays current without anyone pressing 'Check Status'. Probes run in
        parallel (see action_check_status)."""
        stages = self.search([])
        if stages:
            stages.action_check_status()

    def _build_inventory(self):
        """Inventory for this stage — connection comes entirely from its host."""
        if not self.host_id:
            raise UserError(_(
                "This instance has no Server Host set, so there is no IP/port to "
                "connect to. Link it to a host first."
            ))
        return self.host_id._build_inventory()

    @api.model
    def _redact_log(self, text):
        """Strip secrets from text before it is written to ir.logging.

        Discovery emits a base64 JSON blob that can contain the detected Odoo
        master password, so the whole payload is redacted in logs."""
        if not text:
            return text
        return re.sub(r'(ODOO_DISCOVERY_JSON:)[A-Za-z0-9+/=]+', r'\1[redacted]', text)

    @api.model
    def _safe_extra_vars(self, extra_vars):
        """Return a copy of extra_vars with sensitive values redacted."""
        if not extra_vars:
            return extra_vars
        return {
            k: ('********' if k in SENSITIVE_VAR_KEYS else v)
            for k, v in extra_vars.items()
        }

    def _cached_databases(self):
        """Return this instance's cached database list (refreshed in the
        background every 15 min). Falls back to a one-off live refresh of the
        host when the cache is still empty (e.g. right after discovery)."""
        self.ensure_one()
        dbs = [l for l in (self.available_databases or '').splitlines() if l.strip()]
        if not dbs and self.host_id and self.conf_file:
            try:
                self.host_id._refresh_databases()
            except Exception:
                _logger.exception("On-demand DB refresh failed for host %s", self.host_id.id)
            dbs = [l for l in (self.available_databases or '').splitlines() if l.strip()]
        return dbs

    def _cached_modules(self):
        """Return this instance's cached custom-module list (from discovery)."""
        self.ensure_one()
        return [l for l in (self.available_modules or '').splitlines() if l.strip()]

    @api.model
    def _run_ansible_playbook(self, playbook, inventory, extra_vars=None, timeout=None):
        """Run ansible playbook and log the output (with secrets redacted).

        `timeout` (seconds) overrides the default 20-minute cap — daily backups
        of large databases need much longer."""
        timeout = timeout or 1200
        with tempfile.NamedTemporaryFile(mode='w', suffix='.ini', delete=False) as temp_inventory:
            # Restrict perms — inventory may reference key paths / hosts.
            try:
                os.chmod(temp_inventory.name, 0o600)
            except OSError:
                pass
            temp_inventory.write("[all]\n")
            temp_inventory.write(inventory + "\n")
            temp_inventory.flush()

            # Resolve the ansible-playbook binary: explicit env override first,
            # then PATH, then a couple of common locations. Avoids depending on
            # one hardcoded venv path that may not exist on every host.
            ANSIBLE_PLAYBOOK = (
                os.getenv("ANSIBLE_PLAYBOOK")
                or shutil.which("ansible-playbook")
                or next((p for p in (
                    "/usr/bin/ansible-playbook",
                    "/usr/local/bin/ansible-playbook",
                    "/home/odoo/odoo-venv/bin/ansible-playbook",
                ) if os.path.exists(p)), None)
            )
            if not ANSIBLE_PLAYBOOK:
                raise UserError(_(
                    "ansible-playbook executable not found. Install Ansible on "
                    "the Odoo host or set the ANSIBLE_PLAYBOOK environment "
                    "variable to its full path."
                ))
            cmd = [ANSIBLE_PLAYBOOK, playbook, '-i', temp_inventory.name]
            if extra_vars:
                cmd.extend(['--extra-vars', json.dumps(extra_vars)])

            # Build a redacted command/inventory representation for logging only.
            safe_extra_vars = self._safe_extra_vars(extra_vars)
            safe_cmd = [ANSIBLE_PLAYBOOK, playbook, '-i', temp_inventory.name]
            if extra_vars:
                safe_cmd.extend(['--extra-vars', json.dumps(safe_extra_vars)])
            log_msg = (
                f"🔧 Running Ansible Playbook:\nCMD: {' '.join(safe_cmd)}\n"
                f"Inventory:\n{inventory}"
            )
            if safe_extra_vars:
                log_msg += f"\nExtra Vars:\n{json.dumps(safe_extra_vars, indent=2)}"

            try:
                env = os.environ.copy()
                kh = self._known_hosts_file()
                env.update({
                    'ANSIBLE_HOST_KEY_CHECKING': 'True',
                    'ANSIBLE_SSH_ARGS': f'-o StrictHostKeyChecking=accept-new -o UserKnownHostsFile={kh}',
                    # When a task drops to an unprivileged user (become_user: odoo)
                    # Ansible needs setfacl (acl pkg) on the target to share its temp
                    # files; without it newer Ansible hard-fails with a bogus chmod
                    # ACL mode. This fallback makes the temp files world-readable
                    # instead, so actions work even where 'acl' isn't installed.
                    # (Install 'acl' on the servers for the more private ACL path.)
                    'ANSIBLE_SHELL_ALLOW_WORLD_READABLE_TEMP': 'True',
                })

                result = subprocess.run(
                    cmd, capture_output=True, text=True,
                    check=True, timeout=timeout, env=env
                )

                stdout, stderr = result.stdout or '', result.stderr or ''
                full_output = (stdout + '\n' + stderr).strip()
                log_msg += f"\n✅ STDOUT:\n{stdout}\n❗ STDERR:\n{stderr}"

                is_failed = "FAILED!" in stdout or "failed=1" in stdout.lower()

                self.env['ir.logging'].sudo().create({
                    'name': 'Ansible Success' if not is_failed else 'Ansible Partial Failure',
                    'type': 'server',
                    'level': 'warning' if is_failed else 'info',
                    'message': self._redact_log(log_msg),
                    'path': 'server.stage',
                    'func': '_run_ansible_playbook',
                    'line': 0,
                })

                return {'success': not is_failed, 'output': full_output or 'No output received from Ansible.'}

            except subprocess.CalledProcessError as e:
                stdout, stderr = e.stdout or '', e.stderr or ''
                combined_output = (stdout + '\n' + stderr).strip() or str(e)
                log_msg += f"\n❌ ERROR (CalledProcessError):\nSTDOUT:\n{stdout}\nSTDERR:\n{stderr}"

                self.env['ir.logging'].sudo().create({
                    'name': 'Ansible Error',
                    'type': 'server',
                    'level': 'error',
                    'message': self._redact_log(log_msg),
                    'path': 'server.stage',
                    'func': '_run_ansible_playbook',
                    'line': 0,
                })
                return {'success': False, 'output': combined_output}

            except subprocess.TimeoutExpired:
                log_msg += "\n⏰ ERROR: Operation timed out"
                self.env['ir.logging'].sudo().create({
                    'name': 'Ansible Timeout',
                    'type': 'server',
                    'level': 'error',
                    'message': self._redact_log(log_msg),
                    'path': 'server.stage',
                    'func': '_run_ansible_playbook',
                    'line': 0,
                })
                return {'success': False, 'output': 'Operation timed out'}

            except Exception as e:
                log_msg += f"\n🔥 ERROR (Unexpected): {str(e)}"
                self.env['ir.logging'].sudo().create({
                    'name': 'Ansible Unexpected Error',
                    'type': 'server',
                    'level': 'critical',
                    'message': self._redact_log(log_msg),
                    'path': 'server.stage',
                    'func': '_run_ansible_playbook',
                    'line': 0,
                })
                return {'success': False, 'output': str(e)}

            finally:
                os.unlink(temp_inventory.name)

    # ===========================
    # Actions
    # ===========================
    def action_upgrade_module(self):
        self._check_action_access()
        self = self.sudo()
        self.ensure_one()
        dbs = self._cached_databases()
        mods = self._cached_modules()
        ctx = dict(self.env.context, default_stage_id=self.id,
                   db_list=dbs, module_list=mods)
        if dbs:
            ctx['default_database_name'] = dbs[0]
        return {
            'name': 'Upgrade Module',
            'type': 'ir.actions.act_window',
            'res_model': 'server.upgrade.module.wizard',
            'view_mode': 'form',
            'target': 'new',
            'context': ctx,
        }

    def action_view_logs_stream(self):
        self._check_access(GROUP_USER)
        self = self.sudo()
        self.ensure_one()
        # Relative (same-origin) URL so it works whether Odoo is reached via the
        # domain or the server IP — web.base.url may point at a host the browser
        # can't resolve (same issue we fixed for the terminal).
        return {
            'type': 'ir.actions.act_url',
            'target': 'new',
            'url': f'/log/stream/{self.id}',
        }

    def action_view_conf_file(self):
        self._check_access(GROUP_USER)
        self = self.sudo()
        self.ensure_one()
        if not self.conf_file:
            raise UserError(_("No configuration file path is set for this instance. "
                              "Run discovery or set it manually."))
        inventory = self._build_inventory()
        playbook = os.path.join(os.path.dirname(__file__), '../ansible/playbooks/read_conf_file.yml')

        result = self._run_ansible_playbook(playbook, inventory, {'conf_file': self.conf_file})
        if not result['success']:
            raise UserError(_('❌Failed to read configuration file: %s') % result['output'])

        raw_output = result['output']
        match = re.search(r'"msg"\s*:\s*"(.+?)"\s*}', raw_output, re.DOTALL)
        conf_text = match.group(1).encode('utf-8').decode('unicode_escape') if match else raw_output

        # Hide sensitive keys
        hidden_keys = {
            'admin_passwd', 'db_user', 'db_password',
            'db_host', 'uninstall_password',
            'xmlrpc_port', 'longpolling_port', 'db_port'
        }

        filtered_lines = [
            line for line in conf_text.splitlines()
            if line.strip() and not line.strip().startswith(';')
            and line.split('=')[0].strip().lower() not in hidden_keys
        ]

        return {
            'name': _('View Configuration File'),
            'type': 'ir.actions.act_window',
            'res_model': 'server.view.conf.wizard',
            'view_mode': 'form',
            'target': 'new',
            'context': {
                'default_conf_content': '\n'.join(filtered_lines),
                'default_conf_file': self.conf_file,
            }
        }

    def action_pull_code(self):
        self._check_action_access()
        self = self.sudo()
        self.ensure_one()
        return {
            'name': 'Pull Code',
            'type': 'ir.actions.act_window',
            'res_model': 'server.pull.wizard',
            'view_mode': 'form',
            'target': 'new',
            'context': {'default_stage_id': self.id},
        }

    def action_restart_service(self):
        self._check_action_access()
        self = self.sudo()
        self.ensure_one()
        inventory = self._build_inventory()
        playbook = os.path.join(os.path.dirname(__file__), '../ansible/playbooks/restart_service.yml')

        result = self._run_ansible_playbook(playbook, inventory, {'service_name': self.service_name})
        if result['success']:
            self.service_status = True
            return self._notify(_('🔁Service restarted successfully'))
        raise UserError(_('❌Failed to restart service: %s') % result['output'])

    def action_stop_service(self):
        self._check_action_access()
        self = self.sudo()
        self.ensure_one()
        inventory = self._build_inventory()
        playbook = os.path.join(os.path.dirname(__file__), '../ansible/playbooks/stop_service.yml')

        result = self._run_ansible_playbook(playbook, inventory, {'service_name': self.service_name})
        if result['success']:
            self.service_status = False
            return self._notify(_('🛑Service stopped successfully'))
        raise UserError(_('❌Failed to stop service: %s') % result['output'])

    def action_start_service(self):
        self._check_action_access()
        self = self.sudo()
        self.ensure_one()
        inventory = self._build_inventory()
        playbook = os.path.join(os.path.dirname(__file__), '../ansible/playbooks/start_service.yml')

        result = self._run_ansible_playbook(playbook, inventory, {'service_name': self.service_name})
        if result['success']:
            self.service_status = True
            return self._notify(_('🟢Service started successfully'))
        raise UserError(_('❌Failed to start service: %s') % result['output'])

    def action_backup_database(self):
        self._check_action_access()
        self = self.sudo()
        dbs = self._cached_databases()
        ctx = dict(self.env.context, default_stage_id=self.id, db_list=dbs)
        if dbs:
            ctx['default_db_name'] = dbs[0]
        return {
            'name': 'Backup Database',
            'type': 'ir.actions.act_window',
            'res_model': 'server.backup.database.wizard',
            'view_mode': 'form',
            'target': 'new',
            'context': ctx,
        }

    # ===========================
    # Utils
    # ===========================
    def _notify(self, message):
        """Show a success notification that auto-dismisses (no X needed), then
        soft-reload the view. `sticky=False` lets it fade like Odoo's own toasts;
        `soft_reload` refreshes the current view/status without a full browser
        reload."""
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'type': 'success',
                'title': _('Success'),
                'message': message,
                'sticky': False,
                'next': {'type': 'ir.actions.client', 'tag': 'soft_reload'},
            },
        }

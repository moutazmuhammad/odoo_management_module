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
GROUP_USER = 'odoo_server_management.group_user'          # Developer: act on stages
GROUP_OPERATOR = 'odoo_server_management.group_operator'  # Operational: + servers/discover/agent
GROUP_DEVOPS = 'odoo_server_management.group_devops'      # DevOps: everything except settings
GROUP_ADMIN = 'odoo_server_management.group_admin'        # Administrator: + General Settings

# Keys whose values must never be written to ir.logging / process output.
SENSITIVE_VAR_KEYS = {
    'github_token', 'admin_password', 'ssh_password', 'master_pwd',
    'db_password', 'public_key', 'private_key',
}


def _clean_ansible_log(raw, ok):
    """Human-friendly 'Last Operation Details': show ERRORS ONLY.

    On success → '' (the toast and the ✅ state already say it worked, so we don't
    dump the raw PLAY/TASK/RECAP). On failure → the failing step plus the real error
    message(s) extracted from the ansible output (msg/stderr/stdout of the failed
    task), not the whole playbook scaffolding."""
    if ok:
        return ''
    text = (raw or '').strip()
    if not text:
        return ''
    lines = text.splitlines()
    task = ''
    for ln in lines:
        m = re.match(r'\s*TASK \[(.+?)\]', ln)
        if m:
            task = m.group(1)
    chunks = []
    for ln in lines:
        if re.search(r'fatal:|FAILED!|UNREACHABLE!|^ERROR', ln):
            jm = re.search(r'=>\s*(\{.*\})\s*$', ln)
            if jm:
                try:
                    d = json.loads(jm.group(1))
                    for k in ('msg', 'module_stderr', 'stderr', 'stdout', 'module_stdout'):
                        v = d.get(k)
                        if v and str(v).strip():
                            chunks.append(str(v).strip())
                    if chunks:
                        continue
                except Exception:  # noqa: BLE001 — fall back to the raw line
                    pass
            chunks.append(ln.strip())
    # De-duplicate while preserving order; fall back to the raw text if we could not
    # recognise any error markers (so we never hide a real failure).
    detail = '\n'.join(dict.fromkeys(c for c in chunks if c)) or text
    if task:
        detail = 'Failed at: %s\n\n%s' % (task, detail)
    return detail[-4000:]  # bounded but big enough to show a traceback


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
    odoo_bin = fields.Char(string='odoo-bin Path', readonly=True, groups=GROUP_DEVOPS)
    python_bin = fields.Char(string='Python Path', readonly=True, groups=GROUP_DEVOPS)
    # Auto-detected — optional so partially-discovered instances can still be
    # saved and flagged via needs_review; actions validate before use.
    log_file_path = fields.Char(string='Log File Path', groups=GROUP_DEVOPS)
    conf_file = fields.Char(string='Conf File', groups=GROUP_DEVOPS)
    # The nginx site-config file that fronts this instance (matched to its conf by
    # port during discovery); blank when the instance has no nginx vhost.
    nginx_file = fields.Char(string='Nginx File', readonly=True, groups=GROUP_DEVOPS)
    upgrade_module_path = fields.Char(string='Upgrade Module Path', groups=GROUP_DEVOPS)
    http_port = fields.Integer(string='HTTP Port', readonly=True)
    needs_review = fields.Boolean(
        string='Needs Review', default=False,
        help="Set when auto-discovery could not determine every value.",
    )

    odoo_user = fields.Char(string='Odoo User', groups=GROUP_DEVOPS)
    # Stored encrypted at rest; never exposed directly in views. The plaintext
    # is only available through the computed `admin_password` (DevOps only).
    admin_password_enc = fields.Char(string='Odoo Admin Password (encrypted)', groups=GROUP_DEVOPS)
    admin_password = fields.Char(
        string='Odoo Admin Password', groups=GROUP_DEVOPS,
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
        string='Auto-Stop', default=True, groups=GROUP_DEVOPS,
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
    # Status shown on the form. Mirrors the stored odoo_status (kept current by the
    # 5-min cron and the "Check Status" button) — it does NOT probe over SSH on open,
    # so the form loads instantly. Press "Check Status" for a live refresh.
    odoo_status_live = fields.Selection(
        [("running", "🟢 Running"), ("stopped", "🔴 Stopped"), ("unknown", "⚪ Not checked")],
        string="Status", readonly=True, compute='_compute_status_live')
    last_status_check = fields.Datetime(string="Last Status Check", readonly=True)
    # Durable record of the last background action (start/stop/restart/pull/upgrade/
    # backup) so its result/errors survive a reload. The live toast is pushed over
    # the bus (see _run_bg); these fields are the persisted copy shown on the form.
    op_label = fields.Char(string="Last Operation", readonly=True, copy=False)
    op_state = fields.Selection(
        [('running', '⏳ Running'), ('done', '✅ Success'), ('failed', '❌ Failed')],
        string="Last Operation Result", readonly=True, copy=False)
    op_time = fields.Datetime(string="Last Operation At", readonly=True, copy=False)
    op_detail = fields.Text(string="Last Operation Details", readonly=True, copy=False)
    # Cached DB list (newline-separated), refreshed by a background cron every
    # 15 min so the backup/upgrade wizards open instantly without an SSH call.
    available_databases = fields.Text(string="Available Databases", readonly=True, copy=False)
    databases_updated = fields.Datetime(string="Databases Updated", readonly=True)
    # Cached list of the instance's custom modules (newline-separated), detected
    # during discovery — powers the Upgrade wizard's module dropdown.
    available_modules = fields.Text(string="Available Modules", readonly=True, copy=False)
    # Same, for Odoo's bundled (core) modules — offered alongside the custom ones
    # in the Upgrade wizard so e.g. `account`/`web` can be upgraded too.
    available_odoo_modules = fields.Text(string="Available Odoo Modules", readonly=True, copy=False)

    # Stored backups for this stage — REAL transient rows (a regular One2many, so
    # the form renders them reliably). They are (re)listed from the object Space on
    # demand by _load_backups(), triggered by the Backups page "Refresh List" button
    # (action_refresh_backups) — never on open, so the form stays fast.
    backup_file_ids = fields.One2many(
        'server.backup.file', 'stage_id', string='Stored Backups')

    def _load_backups(self):
        """(Re)list this stage's backups from the object Space into real rows."""
        if not self.id:
            return
        try:
            if self.env['server.backup.storage']._keys_set():
                self.env['server.backup.file']._populate_for_stage(self)
        except Exception:  # noqa: BLE001 — never break the form on a storage hiccup
            _logger.exception("Listing backups failed for stage %s", self.id)

    def action_refresh_backups(self):
        """Backups page button: re-list this stage's backups live from the object
        Space so the (read-only) list shows the latest from the bucket on demand.
        Returns a falsy value so the web client just reloads THIS record's data
        (surfacing the new rows) — returning a truthy non-action made it re-run the
        action and jump to another record in a list/pager."""
        self.ensure_one()
        self._check_access(GROUP_USER)
        self._load_backups()
        return False

    def action_open_form(self):
        """Open this stage's full form from the host's inline instance list. Kept
        light (no SSH/S3 on open) so it is instant — status refreshes via the 5-min
        cron / 'Check Status' button, and backups via the Backups 'Refresh List'."""
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': self.name or _('Stage'),
            'res_model': 'server.stage',
            'res_id': self.id,
            'view_mode': 'form',
            'target': 'current',
        }

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
            return int(self.env['ir.config_parameter'].sudo().get_param('server.ssh.port') or 7812)
        except (TypeError, ValueError):
            return 7812

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

    def _compute_status_live(self):
        """Mirror the stored odoo_status onto the form field — NO SSH probe, so the
        form opens instantly. The stored status is kept current by the 5-min cron
        (_cron_refresh_status) and refreshed on demand by action_check_status."""
        for stage in self:
            stage.odoo_status_live = stage.odoo_status or 'unknown'

    def action_check_status(self):
        """Per-stage button: refresh the REAL systemd status over SSH on demand.

        The SSH probe runs in a BACKGROUND thread (its own cursor), so the click
        returns immediately and the UI never blocks/loads. The stored status is
        updated within a moment — reload to see it (the 5-min cron also refreshes
        it). Works for one row or a multi-selection."""
        self._check_access(GROUP_USER)
        stage_ids = [s.id for s in self if s.host_id and s.service_name]
        if not stage_ids:
            return True
        dbname, uid = self.env.cr.dbname, self.env.uid

        def _worker():
            import odoo
            try:
                with odoo.registry(dbname).cursor() as cr:
                    env = api.Environment(cr, uid, {})
                    stages = env['server.stage'].browse(stage_ids).exists()
                    for host in stages.mapped('host_id'):
                        hstages = stages.filtered(lambda s: s.host_id == host)
                        try:
                            host._refresh_status(hstages)
                            cr.commit()
                        except Exception:  # noqa: BLE001
                            cr.rollback()
                            _logger.exception(
                                "Background status check failed for host %s", host.id)
            except Exception:  # noqa: BLE001 — a thread must never crash the worker
                _logger.exception("Background status check thread failed")

        import threading
        threading.Thread(target=_worker, name='odoo-stage-status',
                         daemon=True).start()
        # Plain toast WITHOUT soft_reload: a soft_reload re-runs the current action
        # and, from a list/pager, jumps to another record — so just notify and let
        # the status land via the 30s auto-refresh (or a manual reload).
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'type': 'info', 'title': _('Status'),
                'message': _('🔄 Refreshing status in the background — '
                             'updates in a moment.'),
                'sticky': False,
            },
        }

    # ===========================
    # Background action runner (non-blocking actions with async result toasts)
    # ===========================
    def _op_started_toast(self, label, reload=False):
        """Immediate toast returned by every background action so the button spinner
        clears at once. The `next` action that runs after the toast hides the trigger
        so the user can't re-click and fire the job twice:

        - reload=False (wizard buttons): act_window_close — shuts the wizard modal;
          closing it reloads the parent form, which then reads op_state == 'running'
          and hides the button (a harmless no-op for non-dialog buttons).
        - reload=True (direct form buttons, e.g. discover/run-backup-now): soft_reload
          — refreshes the current view in place so the just-started op_state ==
          'running' hides the button immediately (there is no wizard to close).

        The real result arrives later as a bus toast that soft-reloads again, so the
        button reappears when the op finishes (see _run_bg / stage_ops.js)."""
        nxt = ({'type': 'ir.actions.client', 'tag': 'soft_reload'} if reload
               else {'type': 'ir.actions.act_window_close'})
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'type': 'info',
                'title': _('Working…'),
                'message': _('⏳ %s started — you will be notified when it finishes.')
                           % label,
                'sticky': False,
                'next': nxt,
            },
        }

    @api.model
    def _send_op_bus(self, uid, ok, title, message, sticky=False, url=False, reload=False):
        """Push one operation-result notification to the user's bus channel. The
        frontend service (stage_ops.js) shows it as a toast, triggers the automatic
        download when `url` is set, and refreshes the current view when `reload` is
        set (so a start/stop/restart updates the status badge without a full reload)."""
        self.env['bus.bus']._sendone(
            'server_mgmt_ops_%d' % uid, 'server_mgmt_op',
            {'ok': bool(ok), 'title': title or '', 'message': message or '',
             'sticky': bool(sticky), 'url': url or False, 'reload': bool(reload)})

    def _run_bg(self, label, work, reload=False):
        """Run `work(stage)` in a BACKGROUND thread (own cursor) so the click returns
        instantly and the page never blocks. `work` returns a dict
        {'ok': bool, 'message': str, 'url': str?}; any exception is caught and
        reported as a failure. The outcome is persisted on the stage (op_* fields)
        AND pushed to the user as a bus toast (auto-download when 'url' is present).

        Authorization MUST already have been checked by the caller (in the request,
        as the real user) before calling this."""
        self.ensure_one()
        self.sudo().write({'op_label': label, 'op_state': 'running',
                           'op_time': fields.Datetime.now(), 'op_detail': False})
        stage_id, dbname, uid = self.id, self.env.cr.dbname, self.env.uid

        def _worker():
            import time as _time
            import odoo
            # Phase 1 — run the side-effecting work (ansible) exactly ONCE. We do not
            # persist anything here; `work` returns a dict and only the result write
            # in phase 2 touches the DB (so a retry never re-runs ansible).
            try:
                with odoo.registry(dbname).cursor() as cr:
                    env = api.Environment(cr, uid, {})
                    stage = env['server.stage'].browse(stage_id)
                    if not stage.exists():
                        return
                    try:
                        res = work(stage.sudo()) or {}
                    except Exception as exc:  # noqa: BLE001 — report, never crash
                        _logger.exception("Background op %r failed for stage %s",
                                          label, stage_id)
                        res = {'ok': False, 'message': (str(exc) or repr(exc))[-1500:]}
                    cr.rollback()  # drop ORM reads; the ansible side effect already ran
            except Exception as exc:  # noqa: BLE001
                _logger.exception("Background op %r crashed for stage %s", label, stage_id)
                res = {'ok': False, 'message': (str(exc) or repr(exc))[-1500:]}

            ok = bool(res.get('ok'))
            message = res.get('message') or (
                _('%s finished.') % label if ok else _('%s failed.') % label)
            # Last Operation Details shows ERRORS ONLY: empty on success, the cleaned
            # failure error (no raw PLAY/TASK/RECAP) on failure.
            detail = _clean_ansible_log(res.get('detail') or message, ok)
            url = res.get('url') or False
            title = ('✅ %s' % label) if ok else ('❌ %s' % label)
            vals = {'op_state': 'done' if ok else 'failed',
                    'op_time': fields.Datetime.now(), 'op_detail': detail}
            if ok and res.get('odoo_status'):
                vals['odoo_status'] = res['odoo_status']
                vals['service_status'] = bool(res.get('service_status'))
                vals['last_status_check'] = fields.Datetime.now()
            elif ok and 'service_status' in res:
                vals['service_status'] = bool(res['service_status'])

            # Phase 2 — persist the result + push the toast, retrying on a concurrent
            # update (REPEATABLE READ raises a serialization error when, e.g., the
            # status cron writes the same row at the same moment). Background threads
            # don't get Odoo's request-level retry, so we do it ourselves — otherwise
            # the result write rolls back and the op is stuck on 'running'.
            for attempt in range(5):
                try:
                    with odoo.registry(dbname).cursor() as cr:
                        env = api.Environment(cr, uid, {})
                        stage = env['server.stage'].browse(stage_id)
                        if stage.exists():
                            stage.sudo().write(vals)
                        env['server.stage']._send_op_bus(
                            uid, ok, title, message, sticky=not ok, url=url,
                            reload=reload and ok)  # one bus row per successful commit
                        cr.commit()
                    break
                except Exception:  # noqa: BLE001 — serialization/lock conflict, retry
                    _logger.warning("Persist op result for stage %s: attempt %s failed, "
                                    "retrying", stage_id, attempt + 1)
                    _time.sleep(0.5 * (attempt + 1))
            else:
                _logger.error("Gave up persisting op result for stage %s", stage_id)

        import threading
        threading.Thread(target=_worker, name='odoo-stage-op', daemon=True).start()
        return self._op_started_toast(label)

    @api.model
    def _cron_refresh_status(self):
        """Background job: refresh every instance's real (systemd) status over SSH,
        per host, so the list stays current. Commits per host for resilience."""
        for host in self.env['server.host'].search([]):
            try:
                host._refresh_status()
                self.env.cr.commit()
            except Exception:  # noqa: BLE001
                self.env.cr.rollback()
                _logger.exception("Status refresh failed for host %s", host.id)

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

    def _cached_odoo_modules(self):
        """Return this instance's cached Odoo core-module list (from discovery)."""
        self.ensure_one()
        return [l for l in (self.available_odoo_modules or '').splitlines() if l.strip()]

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
                    # Force a UTF-8 locale so ansible never aborts with "could not
                    # initialize the preferred locale" when the Odoo process happens
                    # to run with an unset/degraded locale (e.g. under a background
                    # thread or a sudo shell). C.UTF-8 is always present.
                    'LC_ALL': 'C.UTF-8',
                    'LANG': 'C.UTF-8',
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
        odoo_mods = self._cached_odoo_modules()
        ctx = dict(self.env.context, default_stage_id=self.id,
                   db_list=dbs, module_list=mods, odoo_module_list=odoo_mods)
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

    def _service_action_work(self, playbook_file, ok_status, ok_message):
        """Build a `work` closure (for _run_bg) that runs a service playbook and, on
        success, RETURNS the resulting status (the worker persists it in its own
        retryable transaction — work itself must not write the DB, since _run_bg
        rolls back phase 1)."""
        playbook = os.path.join(os.path.dirname(__file__),
                                '../ansible/playbooks/%s' % playbook_file)

        def work(stage):
            result = stage._run_ansible_playbook(
                playbook, stage._build_inventory(),
                {'service_name': stage.service_name})
            if result['success']:
                return {'ok': True, 'message': ok_message, 'detail': result['output'],
                        'odoo_status': ok_status, 'service_status': ok_status == 'running'}
            return {'ok': False,
                    'message': _('Failed — see Last Operation Details.'),
                    'detail': result['output']}
        return work

    def action_restart_service(self):
        self._check_action_access()
        self.ensure_one()
        return self.sudo()._run_bg(_('Restart service'), self._service_action_work(
            'restart_service.yml', 'running', _('🔁 Service restarted successfully')),
            reload=True)

    def action_stop_service(self):
        self._check_action_access()
        self.ensure_one()
        return self.sudo()._run_bg(_('Stop service'), self._service_action_work(
            'stop_service.yml', 'stopped', _('🛑 Service stopped successfully')),
            reload=True)

    def action_start_service(self):
        self._check_action_access()
        self.ensure_one()
        return self.sudo()._run_bg(_('Start service'), self._service_action_work(
            'start_service.yml', 'running', _('🟢 Service started successfully')),
            reload=True)

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

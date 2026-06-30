from odoo import models, fields, api, _
from odoo.exceptions import UserError, ValidationError
import os
import json
import re
import yaml

from .stage import GROUP_USER, GROUP_OPERATOR

SAFE_NAME_RE = re.compile(r'^[A-Za-z0-9._-]+$')


class ServerBackupDatabaseWizard(models.TransientModel):
    _name = 'server.backup.database.wizard'
    _description = 'Backup Odoo Database Wizard'

    stage_id = fields.Many2one('server.stage', string='Stage', required=True, readonly=True)
    # The user does ONE thing: either pick the database from the discovered list
    # (db_source='select') OR type it (db_source='manual') — never both.
    db_source = fields.Selection(
        [('select', 'Select from list'), ('manual', 'Type manually')],
        string='Database', required=True,
        default=lambda self: 'select' if self.env.context.get('db_list') else 'manual')
    db_pick = fields.Selection(selection='_sel_databases', string='Database',
                               store=False)
    # Canonical value used by the backup; in 'select' mode it is filled from the
    # picker, in 'manual' mode it is typed. One database per backup.
    db_name = fields.Char(
        string='Database name',
        help="Technical name of the database to back up.")
    backup_format = fields.Selection(
        [('zip', 'Zip (database + filestore)'),
         ('dump', 'Dump (SQL only, no filestore)')],
        string='Format', required=True, default='zip',
        help="Same as Odoo's database manager: 'zip' includes the filestore "
             "(attachments); 'dump' is a pg_dump custom-format file only.",
    )

    @api.model
    def _sel_databases(self):
        # Populated live from the server when the wizard is opened (db_list in
        # the action context).
        return [(d, d) for d in (self.env.context.get('db_list') or [])]

    @api.onchange('db_source')
    def _onchange_db_source(self):
        # Switching method clears the other input so the two are never mixed.
        self.db_name = False
        self.db_pick = False

    @api.onchange('db_pick')
    def _onchange_db_pick(self):
        # In 'select' mode the picked value IS the database (kept in db_name, the
        # canonical field, since db_pick is not stored).
        if self.db_pick:
            self.db_name = self.db_pick

    @api.onchange('db_name')
    def _onchange_db_name_single(self):
        # Immediate feedback: a backup targets exactly one database, so warn as soon
        # as more than one is typed (the hard block is in _check_db_name on submit).
        if self.db_name and len(self.db_name.replace(',', ' ').split()) > 1:
            return {'warning': {
                'title': _("One database only"),
                'message': _("You can back up only one database at a time. "
                             "Please enter a single database name."),
            }}

    @api.constrains('db_name')
    def _check_db_name(self):
        for rec in self:
            db = (rec.db_name or '').strip()
            # Exactly one database per backup.
            if db and len(db.replace(',', ' ').split()) > 1:
                raise ValidationError(_(
                    "Only one database can be backed up at a time — enter a single "
                    "database name."))
            if db and not SAFE_NAME_RE.match(db):
                raise ValidationError(_(
                    "Invalid database name '%s'. Only letters, digits, '.', '_' "
                    "and '-' are allowed."
                ) % rec.db_name)

    def _int_param(self, key, default):
        """Read an int config param, tolerating a non-numeric/empty value."""
        try:
            return int(self.env['ir.config_parameter'].sudo().get_param(key, default=str(default)))
        except (TypeError, ValueError):
            return default

    def _detect_protocol(self, domain):
        """Determine http/https based on IP or port presence."""
        domain = domain.strip()
        ip_only_pattern = r"^\d{1,3}(\.\d{1,3}){3}$"
        ip_port_pattern = r"^\d{1,3}(\.\d{1,3}){3}:\d+$"

        if re.match(ip_port_pattern, domain):
            return f"http://{domain}"

        if re.match(ip_only_pattern, domain):
            return f"http://{domain}:8069"

        if ':' in domain and not domain.startswith(('http://', 'https://')):
            return f"http://{domain}"

        return f"https://{domain}"

    def action_backup(self):
        self.stage_id._check_action_access()
        self.ensure_one()
        if not (self.db_name or '').strip():
            raise UserError(_("Choose a database from the list or type one."))
        self._check_db_name()
        stage = self.stage_id.sudo()
        host = stage.host_id
        Storage = self.env['server.backup.storage']
        if not Storage._keys_set():
            raise UserError(_(
                "Backup storage is not configured. Set the bucket and keys in "
                "Server Management → General Settings → Backups."))

        # Manual backups live under a separate 'manual/' area with a FIXED key per
        # (category, server, db): each press OVERWRITES the previous one, so there
        # is only ever a single latest manual backup per database. The whole
        # 'manual/' area is wiped daily at 03:00 (see _cron_purge_manual). The
        # daily retention prune only touches '<category>/...', so it never affects
        # these.
        category = host.backup_category or 'odex'
        # Same instance segment as the daily backup: the stage's name already holds
        # the nginx domain (or ip:port) resolved at discovery; fall back to host IP.
        seg = host._backup_host_seg(stage.name) or host._backup_host_seg(host.ip)
        ext = 'dump' if self.backup_format == 'dump' else 'zip'
        key = Storage._object_key(
            ['manual', category, seg, '%s.%s' % (self.db_name, ext)])
        # Capture plain values now (the wizard is transient and may be vacuumed
        # before the background job runs).
        db_name = self.db_name
        backup_format = self.backup_format or 'zip'
        filename = '%s.%s' % (db_name, ext)
        url = self._detect_protocol(stage.name)
        admin_password = stage.admin_password
        playbook = os.path.join(os.path.dirname(__file__),
                                '../ansible/playbooks/backup_database.yml')

        def work(stg):
            St = stg.env['server.backup.storage']
            try:
                put_url = St._presign_put(key, ttl=3 * 3600)
            except Exception as exc:  # noqa: BLE001
                return {'ok': False, 'message': _("Could not reach object storage: %s") % exc}
            # Dump on the server and upload straight to the bucket via the pre-signed
            # URL — no object-storage credentials touch the managed server.
            result = stg._run_ansible_playbook(playbook, stg._build_inventory(), {
                'admin_password': admin_password,
                'url': url,
                'database_name': db_name,
                'backup_format': backup_format,
                'presigned_url': put_url,
            }, timeout=3 * 3600)
            if not result['success']:
                return {'ok': False,
                        'message': _('❌ Backup of %s failed — see Last Operation '
                                     'Details.') % db_name,
                        'detail': result['output']}
            try:
                download_url = St._presign_get(key, filename=filename)
            except Exception:  # noqa: BLE001
                download_url = ''
            # Manual backups live under the 'manual/' area and are removed by the
            # daily purge cron (_cron_purge_manual, 03:00) — not after download.
            # The presigned GET sets Content-Disposition attachment, so the frontend
            # service triggers the download automatically when this arrives.
            return {'ok': True, 'url': download_url,
                    'message': _('✅ Backup of %s ready — downloading…') % db_name}

        return stage._run_bg(_('Backup database %s') % db_name, work)

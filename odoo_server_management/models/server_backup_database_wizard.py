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
    db_name = fields.Selection(selection='_sel_databases', string='Database', required=True)
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

    @api.constrains('db_name')
    def _check_db_name(self):
        for rec in self:
            if rec.db_name and not SAFE_NAME_RE.match(rec.db_name.strip()):
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
        self._check_db_name()
        self.ensure_one()
        stage = self.stage_id.sudo()
        host = stage.host_id
        project = host.backup_project_id
        if not project:
            raise UserError(_(
                "This server has no Backup Project assigned. Assign one in "
                "Server Management → Servers → Backups — it provides the bucket "
                "and credentials."))

        # Build the object key. Manual backups go under a separate 'manual/' area
        # so the daily-retention prune (which only touches '<server>/') never
        # deletes them.
        server_slug = self.env['server.host']._slug(host.name)
        seg = (host.ip or '').replace('.', '-')
        ts = fields.Datetime.now().strftime('%Y-%m-%d_%H-%M-%S')
        ext = 'dump' if self.backup_format == 'dump' else 'zip'
        key = project._object_key(
            ['manual', server_slug, seg, self.db_name, '%s.%s' % (ts, ext)])
        try:
            put_url = project._presign_put(key, ttl=3 * 3600)
        except UserError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise UserError(_("Could not reach object storage for project '%s': %s")
                            % (project.name, exc))

        # Dump on the server and upload straight to the bucket via the pre-signed
        # URL — no object-storage credentials touch the managed server.
        inventory = stage._build_inventory()
        playbook = os.path.join(os.path.dirname(__file__),
                                '../ansible/playbooks/backup_database.yml')
        result = stage._run_ansible_playbook(playbook, inventory, {
            'admin_password': stage.admin_password,
            'url': self._detect_protocol(stage.name),
            'database_name': self.db_name,
            'backup_format': self.backup_format or 'zip',
            'presigned_url': put_url,
        }, timeout=3 * 3600)
        if not result['success']:
            raise UserError(_('❌ Failed to backup: %s') % result['output'])

        filename = '%s-%s.%s' % (self.db_name, ts, ext)
        try:
            download_url = project._presign_get(key, filename=filename)
        except Exception:  # noqa: BLE001
            download_url = ''
        # target 'self' + the attachment disposition makes the browser DOWNLOAD
        # the file automatically — no popup to block, and Odoo stays open.
        if download_url:
            return {'type': 'ir.actions.act_url', 'url': download_url, 'target': 'self'}
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': _('Database Backup'),
                'message': _('✅ Backup uploaded to %s.') % project.bucket,
                'type': 'success',
                'sticky': False,
            },
        }

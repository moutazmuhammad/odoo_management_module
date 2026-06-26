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

        inventory = stage._build_inventory()
        playbook = os.path.join(os.path.dirname(__file__), '../ansible/playbooks/backup_database.yml')

        IrConfig = self.env['ir.config_parameter'].sudo()
        result = stage._run_ansible_playbook(playbook, inventory, {
            'admin_password': stage.admin_password,
            'url': self._detect_protocol(stage.name),
            'database_name': self.db_name,
            'backup_format': self.backup_format or 'zip',
            'backup_bucket': IrConfig.get_param('server.backup.bucket', default='odex-backups'),
            'backup_region': IrConfig.get_param('server.backup.region', default='nyc3'),
            'backup_prefix': IrConfig.get_param('server.backup.prefix', default='REAL-TIME'),
            'backup_retention_days': self._int_param('server.backup.retention_days', 1),
            'backup_signed_url_ttl': self._int_param('server.backup.signed_url_ttl', 3600),
        })

        if not result['success']:
            raise UserError(_('❌ Failed to backup: %s') % result['output'])

        try:
            data = None

            # Try to parse structured YAML/JSON directly
            try:
                parsed = yaml.safe_load(result['output'])
                if isinstance(parsed, dict) and "msg" in parsed:
                    if isinstance(parsed["msg"], dict):
                        data = parsed["msg"]  # already JSON object
                    elif isinstance(parsed["msg"], str):
                        # Extract JSON substring inside string
                        json_match = re.search(r'(\{.*\})', parsed["msg"], re.DOTALL)
                        if json_match:
                            data = json.loads(json_match.group(1))
            except Exception:
                pass  # fallback to regex below

            # Fallback: regex search directly in output
            if not data:
                match = re.search(r'"msg":\s*(\{.*?\})', result['output'], re.DOTALL)
                if match:
                    data = json.loads(match.group(1))

            if not data:
                raise UserError(_('Could not find "msg" in response: %s') % result['output'])

            # ✅ Handle success case
            if data.get("status") == "success":
                # This is a short-lived signed URL; do NOT follow redirects, as
                # that would drop the signature query string.
                final_url = data.get("download_url")

                return {
                    'type': 'ir.actions.client',
                    'tag': 'display_notification',
                    'params': {
                        'title': _('Database Backup URL'),
                        'message': _('✅ %s') % final_url,
                        'type': 'success',
                        'sticky': True
                    }
                }
            else:
                raise UserError(_('Backup failed: %s') % result['output'])

        except UserError:
            raise  # our own deliberate messages must not be mislabeled below
        except json.JSONDecodeError as e:
            raise UserError(_('Invalid JSON format: %s') % str(e))
        except Exception as e:
            raise UserError(_('Error parsing backup response: %s') % str(e))

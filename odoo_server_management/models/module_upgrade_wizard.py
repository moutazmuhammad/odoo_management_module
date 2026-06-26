import os
import re
from odoo import models, fields, api, _
from odoo.exceptions import UserError, ValidationError

from .stage import GROUP_USER

# DB names / module names: letters, digits, underscore, dash, dot only.
SAFE_NAME_RE = re.compile(r'^[A-Za-z0-9._-]+$')


class UpgradeModuleWizard(models.TransientModel):
    _name = 'server.upgrade.module.wizard'
    _description = 'Upgrade Odoo Module Wizard'

    stage_id = fields.Many2one('server.stage', string='Stage', required=True)
    database_name = fields.Selection(selection='_sel_databases', string='Database', required=True)
    module_name = fields.Selection(selection='_sel_modules', string='Module', required=True)

    @api.model
    def _sel_databases(self):
        # Populated when the wizard opens (db_list in the action context).
        return [(d, d) for d in (self.env.context.get('db_list') or [])]

    @api.model
    def _sel_modules(self):
        # The instance's custom modules, detected during discovery
        # (module_list in the action context).
        return [(m, m) for m in (self.env.context.get('module_list') or [])]

    @api.constrains('database_name', 'module_name')
    def _check_names(self):
        for rec in self:
            for val in (rec.database_name, rec.module_name):
                if val and not SAFE_NAME_RE.match(val.strip()):
                    raise ValidationError(_(
                        "Invalid value '%s'. Only letters, digits, '.', '_' and "
                        "'-' are allowed."
                    ) % val)

    def action_upgrade(self):
        self.stage_id._check_action_access()
        self._check_names()
        self.ensure_one()
        stage = self.stage_id.sudo()
        if not stage.upgrade_module_path or not stage.odoo_user:
            raise UserError(_("This instance is missing the upgrade module path or "
                              "Odoo user. Run discovery or set them manually."))

        inventory = stage._build_inventory()
        playbook = os.path.join(os.path.dirname(__file__), '../ansible/playbooks/upgrade_module.yml')

        extra_vars = {
            'database_name': self.database_name,
            'module_name': self.module_name,
            'service_name': stage.service_name,
            'upgrade_module_path': stage.upgrade_module_path,
            'odoo_user': stage.odoo_user,
        }

        result = stage._run_ansible_playbook(playbook, inventory, extra_vars)

        if result['success']:
            stage.service_status = True
            return {
                'type': 'ir.actions.client',
                'tag': 'display_notification',
                'params': {
                    'type': 'success',
                    'message': _('✅ Module upgraded successfully'),
                    'next': {'type': 'ir.actions.client', 'tag': 'reload'}
                }
            }
        else:
            raise UserError(_('❌Failed to upgrade module: %s') % result['output'])

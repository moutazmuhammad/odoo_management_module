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
        if not stage.upgrade_module_path or not stage.odoo_user or not stage.conf_file:
            raise UserError(_("This instance is missing the upgrade module path, "
                              "configuration file, or Odoo user. Run discovery or "
                              "set them manually."))

        # Capture plain values now (the wizard is transient and may be vacuumed before
        # the background job runs).
        database_name, module_name = self.database_name, self.module_name
        playbook = os.path.join(os.path.dirname(__file__),
                                '../ansible/playbooks/upgrade_module.yml')

        def work(stg):
            extra_vars = {
                'database_name': database_name,
                'module_name': module_name,
                'service_name': stg.service_name,
                'upgrade_module_path': stg.upgrade_module_path,
                'conf_file': stg.conf_file,
                'odoo_user': stg.odoo_user,
            }
            result = stg._run_ansible_playbook(playbook, stg._build_inventory(), extra_vars)
            # Keep the FULL upgrade log in `detail` (persisted to Last Operation
            # Details) so the user can review it / debug errors after the job ends.
            if result['success']:
                # Return the status hint; _run_bg persists it (work must not write
                # the DB — its phase-1 transaction is rolled back).
                return {'ok': True, 'service_status': True,
                        'message': _('✅ Module %s upgraded on %s')
                        % (module_name, database_name),
                        'detail': result['output']}
            return {'ok': False,
                    'message': _('❌ Upgrade of %s on %s failed — see Last Operation '
                                 'Details.') % (module_name, database_name),
                    'detail': result['output']}

        return stage._run_bg(
            _('Upgrade module %s (%s)') % (module_name, database_name), work)

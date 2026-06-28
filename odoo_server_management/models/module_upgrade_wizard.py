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
    # Free text so any database name can be typed; the picker below fills it from
    # the discovered list as a convenience.
    database_name = fields.Char(
        string='Database', required=True,
        help="Technical name of the database to upgrade. Use 'Select database' to "
             "fill it from the discovered list, or type any name yourself.")
    database_pick = fields.Selection(selection='_sel_databases', string='Select database',
                                     store=False)
    # The actual modules to upgrade: one or more technical names, comma/space
    # separated. Free text, so ANY module can be typed (custom or Odoo core), and
    # several can be upgraded together (Odoo's `-u` takes a comma-separated list).
    module_names = fields.Char(
        string='Modules', required=True,
        help="Technical name(s) of the module(s) to upgrade, comma-separated. "
             "Use 'Add from list' to insert a discovered module, or just type any "
             "name yourself (e.g. account, sale).")
    # Convenience picker: choosing a module appends it to 'Modules' and resets.
    # Offers both the instance's custom modules and Odoo's bundled ones.
    module_pick = fields.Selection(selection='_sel_modules', string='Add from list',
                                   store=False)

    @api.model
    def _sel_databases(self):
        # Populated when the wizard opens (db_list in the action context).
        return [(d, d) for d in (self.env.context.get('db_list') or [])]

    @api.model
    def _sel_modules(self):
        # Custom modules first, then Odoo core modules — both detected during
        # discovery (module_list / odoo_module_list in the action context). The
        # value is the bare technical name; core ones are labelled "(Odoo)".
        seen, opts = set(), []
        for m in (self.env.context.get('module_list') or []):
            if m not in seen:
                seen.add(m)
                opts.append((m, m))
        for m in (self.env.context.get('odoo_module_list') or []):
            if m not in seen:
                seen.add(m)
                opts.append((m, '%s  (Odoo)' % m))
        return opts

    def _module_list(self):
        """Parse the 'Modules' free-text field into a clean list of names
        (accepts comma and/or whitespace separators, drops blanks/duplicates)."""
        self.ensure_one()
        out = []
        for tok in (self.module_names or '').replace(',', ' ').split():
            tok = tok.strip()
            if tok and tok not in out:
                out.append(tok)
        return out

    @api.onchange('database_pick')
    def _onchange_database_pick(self):
        # Picking from the list fills the free-text Database field, then resets.
        if self.database_pick:
            self.database_name = self.database_pick
            self.database_pick = False

    @api.onchange('module_pick')
    def _onchange_module_pick(self):
        # Append the picked module to the free-text field, then clear the picker
        # so the next pick adds another (lets you build a multi-module list).
        if self.module_pick:
            mods = self._module_list()
            if self.module_pick not in mods:
                mods.append(self.module_pick)
            self.module_names = ', '.join(mods)
            self.module_pick = False

    @api.constrains('database_name', 'module_names')
    def _check_names(self):
        for rec in self:
            for val in [rec.database_name] + rec._module_list():
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

        modules = self._module_list()
        if not modules:
            raise UserError(_("Enter at least one module to upgrade."))
        # Capture plain values now (the wizard is transient and may be vacuumed before
        # the background job runs). Odoo's `-u` accepts a comma-separated list, so all
        # requested modules are upgraded in a single run.
        database_name = self.database_name
        modules_arg = ','.join(modules)          # passed straight to `-u`
        modules_label = ', '.join(modules)       # human-readable for toasts/logs
        playbook = os.path.join(os.path.dirname(__file__),
                                '../ansible/playbooks/upgrade_module.yml')

        def work(stg):
            extra_vars = {
                'database_name': database_name,
                'module_name': modules_arg,
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
                        'message': _('✅ Module(s) %s upgraded on %s')
                        % (modules_label, database_name),
                        'detail': result['output']}
            return {'ok': False,
                    'message': _('❌ Upgrade of %s on %s failed — see Last Operation '
                                 'Details.') % (modules_label, database_name),
                    'detail': result['output']}

        return stage._run_bg(
            _('Upgrade module(s) %s (%s)') % (modules_label, database_name), work)

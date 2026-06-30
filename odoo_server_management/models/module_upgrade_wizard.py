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

    # --- Database: do ONE thing — pick it OR type it (never both). ---
    database_source = fields.Selection(
        [('select', 'Select from list'), ('manual', 'Type manually')],
        string='Database', required=True,
        default=lambda self: 'select' if self.env.context.get('db_list') else 'manual')
    database_pick = fields.Selection(selection='_sel_databases', string='Database',
                                     store=False)
    # Canonical value (db_pick is not stored): filled from the picker in 'select'
    # mode, typed in 'manual' mode. One database per upgrade.
    database_name = fields.Char(
        string='Database name',
        help="Technical name of the database to upgrade.")

    # --- Modules: do ONE thing — select several from the list OR type several
    # comma-separated (never both). ---
    module_source = fields.Selection(
        [('select', 'Select from list'), ('manual', 'Type manually')],
        string='Modules', required=True,
        default=lambda self: 'select' if (self.env.context.get('module_list')
                                          or self.env.context.get('odoo_module_list'))
                             else 'manual')
    # In 'select' mode each pick appends to module_names (the canonical, comma-
    # separated value) which is shown read-only; in 'manual' mode it is typed.
    module_names = fields.Char(
        string='Modules',
        help="Technical name(s) of the module(s) to upgrade, comma-separated "
             "(e.g. account, sale).")
    # Convenience picker (select mode): choosing a module appends it and resets, so
    # several can be added. Offers the instance's custom modules and Odoo's core.
    module_pick = fields.Selection(selection='_sel_modules', string='Add module',
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

    @api.onchange('database_source')
    def _onchange_database_source(self):
        # Switching method clears the other input so the two are never mixed.
        self.database_name = False
        self.database_pick = False

    @api.onchange('module_source')
    def _onchange_module_source(self):
        self.module_names = False
        self.module_pick = False

    @api.onchange('database_pick')
    def _onchange_database_pick(self):
        # In 'select' mode the picked value IS the database (kept in database_name,
        # the canonical field, since database_pick is not stored).
        if self.database_pick:
            self.database_name = self.database_pick

    @api.onchange('database_name')
    def _onchange_database_single(self):
        # Immediate feedback: an upgrade targets one database (modules may be many),
        # so warn as soon as more than one DB is typed (hard block is in _check_names).
        if self.database_name and len(self.database_name.replace(',', ' ').split()) > 1:
            return {'warning': {
                'title': _("One database only"),
                'message': _("Only one database can be upgraded at a time. Enter a "
                             "single database name — you can still list several "
                             "modules."),
            }}

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
            db = (rec.database_name or '').strip()
            # Exactly one database per upgrade (modules may be many, the DB may not).
            if db and len(db.replace(',', ' ').split()) > 1:
                raise ValidationError(_(
                    "Only one database can be upgraded at a time — enter a single "
                    "database name."))
            for val in [rec.database_name] + rec._module_list():
                if val and not SAFE_NAME_RE.match(val.strip()):
                    raise ValidationError(_(
                        "Invalid value '%s'. Only letters, digits, '.', '_' and "
                        "'-' are allowed."
                    ) % val)

    def action_upgrade(self):
        self.stage_id._check_action_access()
        self.ensure_one()
        if not (self.database_name or '').strip():
            raise UserError(_("Choose a database from the list or type one."))
        self._check_names()
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

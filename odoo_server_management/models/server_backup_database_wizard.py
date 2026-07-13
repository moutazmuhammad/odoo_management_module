from odoo import models, fields, api, _
from odoo.exceptions import UserError, ValidationError
import re

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

    def action_backup(self):
        self.stage_id._check_action_access('backup')
        self.ensure_one()
        if not (self.db_name or '').strip():
            raise UserError(_("Choose a database from the list or type one."))
        self._check_db_name()
        stage = self.stage_id.sudo()
        if not self.env['server.backup.storage']._keys_set():
            raise UserError(_(
                "Backup storage is not configured. Set the bucket and keys in "
                "Server Management → General Settings → Backups."))
        # Capture the plain value now (the wizard is transient and may be vacuumed
        # before the background job runs).
        db_name = self.db_name.strip()

        # Take the backup exactly the way the DAILY backup does — a direct pg_dump
        # streamed into an Odoo-format zip (DB + filestore + manifest), uploaded via
        # a pre-signed URL — instead of Odoo's /web/database/backup endpoint, which
        # returns an HTTP-200 HTML error page (silently saved as a corrupt zip)
        # whenever its own server-side pg_dump fails. The manual backup lands under a
        # FIXED manual/<category>/<server>/<db>.zip key (overwritten each press) and
        # the whole manual/ area is wiped daily at 03:00 by _cron_purge_manual.
        def work(stg):
            return stg.host_id._run_manual_backup(stg, db_name)

        return stage._run_bg(_('Backup database %s') % db_name, work)

import base64
import json
import logging

from odoo import models, fields, api, _
from odoo.exceptions import UserError

from .stage import GROUP_DEVOPS

_logger = logging.getLogger(__name__)


class ServerStageDeleteWizard(models.TransientModel):
    """Confirmation dialog for permanently removing a broken/old/unused instance —
    from the manager AND (optionally) from the server: its systemd service, conf and
    log, its nginx site, and its database(s) + filestore. Requires typing the
    instance name to confirm, and each destructive part is opt-in."""
    _name = 'server.stage.delete.wizard'
    _description = 'Delete / Decommission Instance'

    stage_id = fields.Many2one('server.stage', string='Instance',
                               required=True, readonly=True, ondelete='cascade')
    stage_name = fields.Char(related='stage_id.name', string='Instance', readonly=True)
    host_name = fields.Char(related='stage_id.host_id.name', string='Server', readonly=True)
    service_name = fields.Char(related='stage_id.service_name', string='Service', readonly=True)
    databases = fields.Text(related='stage_id.available_databases',
                            string='Databases', readonly=True)

    remove_service = fields.Boolean(
        string="Remove the service (stop, disable, delete its systemd unit + conf + log)",
        default=True)
    remove_nginx = fields.Boolean(string="Remove its nginx site", default=False)
    drop_database = fields.Boolean(
        string="DROP its database(s) — irreversible", default=False)
    remove_filestore = fields.Boolean(string="Delete its filestore", default=False)

    confirm = fields.Char(string="Type the instance name to confirm")

    @api.onchange('drop_database')
    def _onchange_drop_database(self):
        if not self.drop_database:
            self.remove_filestore = False

    def action_delete(self):
        self.ensure_one()
        stage = self.stage_id
        if not stage.exists():
            return {'type': 'ir.actions.act_window_close'}
        # Gate the REAL user (before the sudo below elevates to superuser).
        stage._check_access(GROUP_DEVOPS)
        if (self.confirm or '').strip() != (stage.name or '').strip():
            raise UserError(_(
                "To confirm, type the instance name exactly: %s") % stage.name)
        return stage.sudo()._decommission({
            'remove_service': self.remove_service,
            'remove_nginx': self.remove_nginx,
            'drop_database': self.drop_database,
            'remove_filestore': self.remove_filestore,
        })

import re

from odoo import models, fields, api, _
from odoo.exceptions import UserError

# config-parameter keys
PARAM_DOMAINS = 'server.signup.allowed_domains'


class ResUsers(models.Model):
    _inherit = 'res.users'

    # Stages this user is explicitly denied View on (a grant row with can_read
    # OFF). Used by the server.stage record rule to hide them. Non-stored (it is
    # per-user and small) and computed with sudo so a plain Developer can resolve
    # their own record rule without ACL on the grant model.
    stage_denied_ids = fields.Many2many(
        'server.stage', compute='_compute_stage_denied_ids',
        string='Hidden Stages')

    def _compute_stage_denied_ids(self):
        Access = self.env['server.stage.access'].sudo()
        for user in self:
            rows = Access.search([('user_id', '=', user.id), ('can_read', '=', False)])
            user.stage_denied_ids = rows.mapped('stage_id')

    def _sync_access_grid(self):
        """Enrol these users into the Access grid across all stages (a no-op for
        non-Developers). Wrapped so a grid hiccup never breaks user save/login."""
        try:
            self.env['server.stage.access'].sudo()._sync_users(self)
        except Exception:  # noqa: BLE001
            pass

    @api.model_create_multi
    def create(self, vals_list):
        users = super().create(vals_list)
        users._sync_access_grid()
        return users

    def write(self, vals):
        res = super().write(vals)
        # Group membership can change who is a Developer -> refresh their rows.
        if 'groups_id' in vals:
            self._sync_access_grid()
        return res

    @api.model
    def _signup_allowed_domains(self):
        """Parsed list of allowed signup email domains (empty = no restriction)."""
        raw = self.env['ir.config_parameter'].sudo().get_param(PARAM_DOMAINS, default='')
        return [d.strip().lower().lstrip('@')
                for d in re.split(r'[,\s;]+', raw or '') if d.strip()]

    @api.model
    def _check_signup_email_domain(self, email):
        domains = self._signup_allowed_domains()
        if not domains:
            return  # unrestricted
        addr = (email or '').strip().lower()
        if '@' not in addr or addr.rsplit('@', 1)[1] not in domains:
            raise UserError(_(
                "Sign up is only allowed with an email address ending in: %s"
            ) % ", ".join('@' + d for d in domains))

    @api.model
    def _signup_create_user(self, values):
        """Self-signup hook: validate the email domain, then grant the new user
        the internal-user + module 'User' role only (no operator/admin)."""
        self._check_signup_email_domain(values.get('login') or values.get('email'))
        user = super()._signup_create_user(values)
        try:
            groups = (self.env.ref('base.group_user')
                      | self.env.ref('odoo_server_management.group_user'))
            user.sudo().write({'groups_id': [(4, g.id) for g in groups]})
        except Exception:
            # never let role-assignment break the signup transaction
            pass
        return user

import re

from odoo import models, api, _
from odoo.exceptions import UserError

# config-parameter keys
PARAM_DOMAINS = 'server.signup.allowed_domains'


class ResUsers(models.Model):
    _inherit = 'res.users'

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

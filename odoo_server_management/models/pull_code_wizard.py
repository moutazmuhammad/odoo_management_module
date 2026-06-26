import os
import re
from odoo import models, fields, _
from odoo.exceptions import UserError

from .stage import GROUP_USER


def _normalize_repo_url(url):
    """Return a clean https URL (no embedded creds) so the playbook can inject
    `user:token@`. Handles scp-style remotes too: git@host:org/repo -> https."""
    u = (url or '').strip()
    m = re.match(r'^[^@/]+@([^:/]+):(.+)$', u)        # scp form git@host:org/repo
    if m:
        return 'https://%s/%s' % (m.group(1), m.group(2))
    u = re.sub(r'^(https?://)[^@/]+@', r'\1', u)      # strip https://user[:tok]@
    if not re.match(r'^https?://', u):
        u = 'https://' + u
    return u


class PullCodeWizard(models.TransientModel):
    _name = 'server.pull.wizard'
    _description = 'Pull Code Wizard'

    stage_id = fields.Many2one('server.stage', string='Stage', required=True, readonly=True)
    repo_branch_path_id = fields.Many2one(
        'server.stage.repo.branch.path',
        string='Repository & Branch',
        required=True,
        domain="[('stage_id', '=', stage_id)]"
    )

    def action_confirm_pull(self):
        self.stage_id._check_action_access()
        self = self.sudo()
        self.ensure_one()
        repo = self.repo_branch_path_id.repository_id
        branch = self.repo_branch_path_id.branch_id.name
        path = self.repo_branch_path_id.pull_path
        service_name = self.stage_id.service_name
        odoo_user = self.stage_id.odoo_user
        if not odoo_user:
            raise UserError(_("This instance has no Odoo user set, so a pull would "
                              "run as the wrong user. Run discovery or set it first."))
        inventory = self.stage_id._build_inventory()

        IrConfig = self.env['ir.config_parameter'].sudo()
        github_user = IrConfig.get_param('server.github.user')
        github_token = self.env['server.stage']._get_secret_param('server.github.token')

        playbook = os.path.join(os.path.dirname(__file__), '../ansible/playbooks/pull_code.yml')
        extra_vars = {
            'repo_url': _normalize_repo_url(repo.url),
            'branch_name': branch,
            'path': path,
            'github_user': github_user,
            'github_token': github_token,
            'service_name': service_name,
            'odoo_user': odoo_user,
        }

        result = self.stage_id._run_ansible_playbook(playbook, inventory, extra_vars)
        if result['success']:
            return {
                'type': 'ir.actions.client',
                'tag': 'display_notification',
                'params': {
                    'type': 'success',
                    'message': _('📥Code pulled successfully'),
                    'next': {'type': 'ir.actions.client', 'tag': 'reload'}
                }
            }
        else:
            raise UserError(_('❌Failed to pull code: %s') % result['output'])

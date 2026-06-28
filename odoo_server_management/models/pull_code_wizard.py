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
        stage = self.stage_id
        repo = self.repo_branch_path_id.repository_id
        branch = self.repo_branch_path_id.branch_id.name
        path = self.repo_branch_path_id.pull_path
        if not stage.odoo_user:
            raise UserError(_("This instance has no Odoo user set, so a pull would "
                              "run as the wrong user. Run discovery or set it first."))

        IrConfig = self.env['ir.config_parameter'].sudo()
        # Capture plain values now (wizard is transient — may be vacuumed before the
        # background job runs).
        extra_vars = {
            'repo_url': _normalize_repo_url(repo.url),
            'branch_name': branch,
            'path': path,
            'github_user': IrConfig.get_param('server.github.user'),
            'github_token': self.env['server.stage']._get_secret_param('server.github.token'),
            'service_name': stage.service_name,
            'odoo_user': stage.odoo_user,
        }
        playbook = os.path.join(os.path.dirname(__file__),
                                '../ansible/playbooks/pull_code.yml')

        def work(stg):
            result = stg._run_ansible_playbook(playbook, stg._build_inventory(), extra_vars)
            if result['success']:
                return {'ok': True, 'message': _('📥 Code pulled successfully (%s @ %s)')
                        % (path, branch)}
            return {'ok': False, 'message': result['output']}

        return stage._run_bg(_('Pull code (%s)') % branch, work)

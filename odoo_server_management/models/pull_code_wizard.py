import os
import re
from odoo import models, fields, api, _
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
        string='Repository & Path',
        required=True,
        domain="[('stage_id', '=', stage_id)]"
    )
    repository_id = fields.Many2one(
        'server.repository', related='repo_branch_path_id.repository_id', readonly=True)
    # Pick ANY branch of the selected repository (discovery records them all), not
    # just the one currently checked out on the server.
    branch_id = fields.Many2one(
        'server.repository.branch', string='Branch',
        domain="[('repository_id', '=', repository_id)]")

    @api.onchange('repo_branch_path_id')
    def _onchange_repo_branch_path_id(self):
        # Default the branch to the one currently deployed at this path.
        self.branch_id = self.repo_branch_path_id.branch_id

    def action_confirm_pull(self):
        self.stage_id._check_action_access()
        self = self.sudo()
        self.ensure_one()
        stage = self.stage_id
        repo = self.repo_branch_path_id.repository_id
        # The user-chosen branch wins; fall back to the path's current branch.
        branch = (self.branch_id.name or self.repo_branch_path_id.branch_id.name)
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
            # Full pull log kept in `detail` (Last Operation Details) for review/debug.
            if result['success']:
                return {'ok': True, 'detail': result['output'],
                        'message': _('📥 Code pulled successfully (%s @ %s)')
                        % (path, branch)}
            return {'ok': False,
                    'message': _('❌ Pull code failed — see Last Operation Details.'),
                    'detail': result['output']}

        return stage._run_bg(_('Pull code (%s)') % branch, work)

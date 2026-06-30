from odoo import models, fields, api


class GitRepository(models.Model):
    _name = 'server.repository'
    _description = 'Git Repository'
    _order = 'name'

    name = fields.Char(string='Repository Name', required=True)
    url = fields.Char(string='GitHub URL', required=True)
    branches = fields.One2many('server.repository.branch', 'repository_id', string='Branches')


class GitRepositoryBranch(models.Model):
    _name = 'server.repository.branch'
    _description = 'Git Repository Branch'

    name = fields.Char(string='Branch Name', required=True)
    repository_id = fields.Many2one('server.repository', string='Repository', required=True, ondelete='cascade')
    
class StageRepoBranchPath(models.Model):
    _name = 'server.stage.repo.branch.path'
    _description = 'Stage Repo Branch Path'
    _rec_name = 'display_name'

    stage_id = fields.Many2one('server.stage', string='Stage', required=True, ondelete='cascade')
    repository_id = fields.Many2one('server.repository', string='Repository', required=True, ondelete='cascade')
    branch_id = fields.Many2one('server.repository.branch', string='Branch', required=True, ondelete='cascade', domain="[('repository_id', '=', repository_id)]")
    pull_path = fields.Char(string='Pull Path on Server', required=True)
    display_name = fields.Char(string='Display Name', compute='_compute_display_name', store=True)

    # The commit this checkout's HEAD is currently on. Filled during discovery,
    # refreshed daily by cron, and on demand via the "Get Commit" button.
    current_commit = fields.Char(string='Current Commit', readonly=True,
        help="Full SHA of the commit this checkout is currently on.")
    current_commit_short = fields.Char(string='Commit', readonly=True,
        help="Short SHA of the current commit.")
    commit_subject = fields.Char(string='Commit Message', readonly=True,
        help="Subject line of the current commit.")
    commit_author = fields.Char(string='Commit Author', readonly=True)
    commit_date = fields.Char(string='Commit Date', readonly=True,
        help="Commit date of the current HEAD, as reported by git (ISO 8601).")
    commit_checked = fields.Datetime(string='Commit Checked', readonly=True,
        help="When this commit information was last refreshed.")

    @api.depends('repository_id.name', 'branch_id.name', 'pull_path')
    def _compute_display_name(self):
        for rec in self:
            repo = rec.repository_id.name or ''
            branch = rec.branch_id.name or ''
            # Include the checkout folder so the SAME repo cloned at different paths
            # (each on its own branch) is distinguishable in the Pull wizard.
            leaf = (rec.pull_path or '').rstrip('/').split('/')[-1]
            rec.display_name = f"{repo} [{branch}]" + (f" — {leaf}" if leaf else "")


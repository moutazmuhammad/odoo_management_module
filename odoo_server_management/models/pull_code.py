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

    @api.depends('repository_id.name', 'branch_id.name')
    def _compute_display_name(self):
        for rec in self:
            repo = rec.repository_id.name or ''
            branch = rec.branch_id.name or ''
            rec.display_name = f"{repo} [{branch}]"


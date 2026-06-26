from odoo import models, fields

class StageBranch(models.Model):
    _name = 'server.stage.branch'
    _description = 'Stage Branch'

    branch_name = fields.Char(string='Branch Name', required=True)
    pull_path = fields.Char(string='Pull Path', required=True)
    stage = fields.Many2one('server.stage', string='Stage', required=True, ondelete='cascade')
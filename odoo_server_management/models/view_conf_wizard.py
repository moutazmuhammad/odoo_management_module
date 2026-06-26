from odoo import models, fields

class ServerViewConfWizard(models.TransientModel):
    _name = 'server.view.conf.wizard'
    _description = 'View Configuration File Wizard'

    conf_file = fields.Char(string='File', readonly=True)
    conf_content = fields.Text(string='Configuration File Content', readonly=True)

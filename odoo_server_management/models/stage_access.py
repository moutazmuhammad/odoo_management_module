from odoo import models, fields, api, _


class StageAccess(models.Model):
    """Per-user, per-stage action grant. A row is the authoritative permission
    set for one (user, stage) pair; its four toggles decide which operational
    buttons (Pull / Backup / Upgrade / Start-Stop-Restart) that user gets.

    When NO row exists for a (user, stage) pair, the default depends on the
    stage type:

      * **Client Server** stage → DENY everything. A Developer sees the instance
        and its logs but cannot act until a row grants the action (opt-in).
      * **Normal (non-client)** stage → ALLOW everything, as before. Add a row and
        switch actions OFF to take permissions away from a specific user (opt-out).

    A brand-new row starts with every action ON (see the field defaults); the
    DevOps/Admin then switches OFF whatever this user should not do. Managed only
    from the 'Access' page. DevOps and Admin bypass this model entirely — they
    always have every action on every stage.
    """
    _name = 'server.stage.access'
    _description = 'Stage Access Grant'
    _rec_name = 'user_id'
    _order = 'stage_id, user_id'

    stage_id = fields.Many2one(
        'server.stage', string='Stage', required=True, ondelete='cascade',
        index=True, help="The instance this grant applies to.")
    client_stage = fields.Boolean(
        related='stage_id.client_stage', string='Client Server', store=True)
    user_id = fields.Many2one(
        'res.users', string='User', required=True, ondelete='cascade', index=True,
        help="The user being granted actions on this client stage.")

    # A fresh grant starts fully allowed; the admin then switches OFF whatever
    # this user should not do.
    #   can_read = see the stage at all (in lists/forms + its logs/conf). Default
    #   ON for every stage; switch OFF to HIDE this stage from this user.
    can_read = fields.Boolean(string='View', default=True)
    can_pull = fields.Boolean(string='Pull Code', default=True)
    can_backup = fields.Boolean(string='Backup', default=True)
    can_upgrade = fields.Boolean(string='Upgrade', default=True)
    can_control = fields.Boolean(string='Start / Stop / Restart', default=True)

    _sql_constraints = [
        ('stage_user_uniq', 'unique(stage_id, user_id)',
         'This user already has an access row on this client stage.'),
    ]

    def name_get(self):
        return [(r.id, '%s → %s' % (r.stage_id.name or '', r.user_id.name or ''))
                for r in self]

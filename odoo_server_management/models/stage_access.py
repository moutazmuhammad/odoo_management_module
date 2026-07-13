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

    # ------------------------------------------------------------------
    # Matrix materialisation (Jenkins-style grid)
    # ------------------------------------------------------------------
    @api.model
    def _developer_users(self):
        """Users the grid is about: module Developers who are NOT DevOps/Admin
        (DevOps/Admin bypass grants, so putting them in the grid is misleading)."""
        group_user = self.env.ref('odoo_server_management.group_user')
        group_devops = self.env.ref('odoo_server_management.group_devops')
        return group_user.users - group_devops.users

    @api.model
    def _create_missing(self, users, stages):
        """Create a grant row for every (developer, stage) pair in
        users × stages that doesn't already have one. `users` is filtered down
        to actual Developers (DevOps/Admin are skipped — they bypass grants).
        Rows are created with the current effective default (View always ON; the
        four actions ON for a normal instance, OFF for a Client Server), so
        materialising the grid changes no behaviour until a toggle is flipped.
        Returns the number of rows created."""
        users = users & self._developer_users()
        stages = stages.sudo()
        if not users or not stages:
            return 0
        Access = self.sudo()
        existing = Access.search_read(
            [('user_id', 'in', users.ids), ('stage_id', 'in', stages.ids)],
            ['user_id', 'stage_id'])
        have = {(r['user_id'][0], r['stage_id'][0]) for r in existing}
        vals = []
        for st in stages:
            allow = not st.client_stage
            for u in users:
                if (u.id, st.id) not in have:
                    vals.append({
                        'stage_id': st.id, 'user_id': u.id, 'can_read': True,
                        'can_pull': allow, 'can_backup': allow,
                        'can_upgrade': allow, 'can_control': allow,
                    })
        if vals:
            Access.create(vals)
        return len(vals)

    @api.model
    def _sync_matrix(self):
        """Full grid: every developer × every stage. Used by the menu and the
        Refresh button. Cheap when already materialised (creates nothing)."""
        return self._create_missing(
            self._developer_users(), self.env['server.stage'].sudo().search([]))

    @api.model
    def _sync_stages(self, stages):
        """Rows for NEW stages (all developers). Called on server.stage create
        (incl. discovery), so a freshly-discovered instance joins the grid at
        once."""
        return self._create_missing(self._developer_users(), stages)

    @api.model
    def _sync_users(self, users):
        """Rows for NEW/changed users (all stages). Called on res.users
        create/write, so a user who just became a Developer joins the grid."""
        return self._create_missing(users, self.env['server.stage'].sudo().search([]))

    def action_sync_matrix(self):
        """Header button on the Access list: (re)build the grid, then reopen it
        so any newly-added users or stages appear as rows."""
        created = self._sync_matrix()
        action = self.env['ir.actions.act_window']._for_xml_id(
            'odoo_server_management.action_stage_access')
        action['context'] = {'search_default_group_stage': 1,
                             'search_default_client': 1}
        msg = (_("Added %s new row(s).") % created) if created else _("Grid is already up to date.")
        action['name'] = 'Access'
        self.env['bus.bus']._sendone(
            self.env.user.partner_id, 'simple_notification',
            {'type': 'success', 'title': _('Access'), 'message': msg})
        return action

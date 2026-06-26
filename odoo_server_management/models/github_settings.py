import base64

from odoo import models, fields, api, exceptions, _

class GithubSettings(models.TransientModel):
    _name = 'server.github.settings'
    _description = 'Server Management Settings'
    _inherit = 'res.config.settings'

    github_user = fields.Char(string="GitHub Username")
    # Write-only: never echoed back to the form (see get_values). Leave blank to
    # keep the stored token; type a new one to replace it.
    github_token = fields.Char(string="GitHub Token")
    github_token_is_set = fields.Boolean(string="GitHub Token Configured", readonly=True)

    # Single global SSH key + defaults used to reach every managed server.
    # Access is key-only: paste the private key here (or point to a key file on
    # the Odoo host). The matching public key must already be in each server's
    # authorized_keys (pre-provisioned). No password is ever used.
    ssh_default_user = fields.Char(string="Default SSH User", default="root")
    ssh_default_port = fields.Integer(string="Default SSH Port", default=22)
    # Preferred: upload the key file instead of pasting it as text.
    ssh_private_key_upload = fields.Binary(
        string="Upload SSH Private Key",
        help="Upload the single global private key file (PEM/OpenSSH, "
             "unencrypted). It is validated, encrypted at rest, and written to "
             "a 0600 file on the Odoo host. Leave empty to keep the current key.",
    )
    ssh_private_key_filename = fields.Char(string="Key Filename")
    # Write-only: never echoed back to the form (see get_values). Leave blank to
    # keep the stored key; upload/paste a new one to replace it.
    ssh_private_key = fields.Text(
        string="SSH Private Key (paste alternative)",
        help="Alternative to uploading: paste the single global private key "
             "(PEM/OpenSSH). It is written to a 0600 file on the Odoo host and "
             "used for all servers.",
    )
    ssh_key_is_set = fields.Boolean(string="SSH Key Configured", readonly=True)
    ssh_private_key_file = fields.Char(
        string="SSH Private Key File (optional)",
        help="Optional: absolute path to an existing private key file on the "
             "Odoo host. If set, it takes precedence over the pasted key.",
    )

    # Object-storage target for database backups (M2/M3). No infra is hardcoded
    # in the playbook anymore; backups are uploaded privately and downloaded via
    # a short-lived signed URL.
    backup_bucket = fields.Char(string="Backup Bucket", default="odex-backups")
    backup_region = fields.Char(string="Backup Region", default="nyc3")
    backup_prefix = fields.Char(
        string="Backup Prefix", default="REAL-TIME",
        help="Folder/key prefix inside the bucket where dumps are stored.",
    )
    backup_retention_days = fields.Integer(
        string="Backup Retention (days)", default=1,
        help="Backups older than this many days are pruned on each run.",
    )
    backup_signed_url_ttl = fields.Integer(
        string="Signed URL TTL (seconds)", default=3600,
        help="Lifetime of the signed download URL returned after a backup.",
    )

    # Self-signup: when enabled, anyone with an allowed-domain email can sign up
    # and gets the module's "User" role only.
    # Auto-stop: instances running longer than this are stopped daily (only on
    # servers with "Stop Instances" on, for instances with "Auto-Stop" on).
    auto_stop_days = fields.Integer(string="Auto-Stop Instances After (days)", default=7)

    signup_enabled = fields.Boolean(string="Enable User Signup")
    signup_allowed_domains = fields.Char(
        string="Allowed Signup Email Domains",
        help="Comma-separated list, e.g. exp-sa.com, odex.sa — only emails "
             "ending with one of these may sign up. Leave empty to allow any "
             "domain. New signups always get the 'User' role only.",
    )

    def get_values(self):
        res = super(GithubSettings, self).get_values()
        IrConfig = self.env['ir.config_parameter'].sudo()
        Stage = self.env['server.stage']
        res.update(
            github_user=IrConfig.get_param('server.github.user', default=''),
            # Secrets are write-only — never echo them back to the form. Show
            # only whether one is configured.
            github_token='',
            github_token_is_set=bool(Stage._get_secret_param('server.github.token')),
            ssh_default_user=IrConfig.get_param('server.ssh.user', default='root'),
            ssh_default_port=int(IrConfig.get_param('server.ssh.port', default='22') or 22),
            ssh_private_key='',
            ssh_key_is_set=bool(Stage._get_secret_param('server.ssh.private_key')
                                or IrConfig.get_param('server.ssh.private_key_file')),
            ssh_private_key_file=IrConfig.get_param('server.ssh.private_key_file', default=''),
            backup_bucket=IrConfig.get_param('server.backup.bucket', default='odex-backups'),
            backup_region=IrConfig.get_param('server.backup.region', default='nyc3'),
            backup_prefix=IrConfig.get_param('server.backup.prefix', default='REAL-TIME'),
            backup_retention_days=int(IrConfig.get_param('server.backup.retention_days', default='1') or 1),
            backup_signed_url_ttl=int(IrConfig.get_param('server.backup.signed_url_ttl', default='3600') or 3600),
            auto_stop_days=int(IrConfig.get_param('server.autostop.days', default='7') or 7),
            signup_enabled=(IrConfig.get_param('auth_signup.invitation_scope', default='b2b') == 'b2c'),
            signup_allowed_domains=IrConfig.get_param('server.signup.allowed_domains', default='exp-sa.com, odex.sa'),
        )
        return res

    def set_values(self):
        super(GithubSettings, self).set_values()
        IrConfig = self.env['ir.config_parameter'].sudo()
        Stage = self.env['server.stage']
        # Resolve a NEW key only if one was provided this save (upload preferred,
        # else paste). A blank field means "keep the stored key", never wipe it —
        # the form no longer round-trips the secret.
        new_key = ''
        if self.ssh_private_key_upload:
            try:
                new_key = base64.b64decode(self.ssh_private_key_upload).decode('utf-8')
            except (ValueError, UnicodeDecodeError):
                raise exceptions.UserError(_(
                    "The uploaded private key file is not valid text. Upload an "
                    "unencrypted PEM/OpenSSH private key file (not a .ppk, a "
                    "binary, or a passphrase-protected key)."
                ))
        elif self.ssh_private_key:
            new_key = self.ssh_private_key
        if new_key:
            # Validate up front so a bad key fails here with a clear message.
            Stage._validate_private_key(new_key)

        IrConfig.set_param('server.github.user', self.github_user or '')
        IrConfig.set_param('server.ssh.user', self.ssh_default_user or 'root')
        IrConfig.set_param('server.ssh.port', str(self.ssh_default_port or 22))
        IrConfig.set_param('server.ssh.private_key_file', self.ssh_private_key_file or '')
        IrConfig.set_param('server.backup.bucket', self.backup_bucket or 'odex-backups')
        IrConfig.set_param('server.backup.region', self.backup_region or 'nyc3')
        IrConfig.set_param('server.backup.prefix', self.backup_prefix or 'REAL-TIME')
        IrConfig.set_param('server.backup.retention_days', str(self.backup_retention_days or 1))
        IrConfig.set_param('server.backup.signed_url_ttl', str(self.backup_signed_url_ttl or 3600))
        IrConfig.set_param('server.autostop.days', str(self.auto_stop_days or 0))
        # Signup: toggle Odoo's free signup + store the allowed-domain list.
        IrConfig.set_param('auth_signup.invitation_scope', 'b2c' if self.signup_enabled else 'b2b')
        IrConfig.set_param('auth_signup.allow_uninvited', 'True' if self.signup_enabled else 'False')
        IrConfig.set_param('server.signup.allowed_domains', self.signup_allowed_domains or '')

        # Secrets: only update when a new value was entered (blank = keep).
        if self.github_token:
            Stage._set_secret_param('server.github.token', self.github_token)
        if new_key:
            Stage._set_secret_param('server.ssh.private_key', new_key)
            Stage._materialize_key(new_key)



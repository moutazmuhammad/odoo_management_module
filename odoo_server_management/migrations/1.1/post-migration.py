"""Encrypt-at-rest migration (M5).

Encrypts any pre-existing plaintext secrets:
  - server.stage.admin_password  -> admin_password_enc (then drop old column)
  - ir.config_parameter: server.ssh.private_key, server.github.token

Idempotent: values already carrying the `enc$` marker are skipped, and the
decrypt helper treats unmarked values as legacy plaintext.
"""
import logging
from odoo import api, SUPERUSER_ID

_logger = logging.getLogger(__name__)


def migrate(cr, version):
    env = api.Environment(cr, SUPERUSER_ID, {})
    Stage = env['server.stage']

    # 1) admin_password: the field became non-stored/computed, so its old column
    #    is now an orphan still holding plaintext. Encrypt into the new column,
    #    then drop the plaintext column entirely.
    cr.execute("""
        SELECT column_name FROM information_schema.columns
        WHERE table_name = 'server_stage' AND column_name = 'admin_password'
    """)
    if cr.fetchone():
        cr.execute("""
            SELECT id, admin_password FROM server_stage
            WHERE admin_password IS NOT NULL AND admin_password != ''
        """)
        rows = cr.fetchall()
        for rec_id, plaintext in rows:
            if plaintext.startswith(Stage._SECRET_PREFIX):
                continue
            enc = Stage._encrypt_secret(plaintext)
            cr.execute(
                "UPDATE server_stage SET admin_password_enc = %s WHERE id = %s",
                (enc, rec_id),
            )
        _logger.info("Encrypted admin_password for %d stage(s).", len(rows))
        cr.execute("ALTER TABLE server_stage DROP COLUMN IF EXISTS admin_password")
        _logger.info("Dropped plaintext admin_password column.")

    # 2) Config-parameter secrets.
    ICP = env['ir.config_parameter'].sudo()
    for key in ('server.ssh.private_key', 'server.github.token'):
        raw = ICP.get_param(key, default='')
        if raw and not raw.startswith(Stage._SECRET_PREFIX):
            ICP.set_param(key, Stage._encrypt_secret(raw))
            _logger.info("Encrypted config parameter %s.", key)

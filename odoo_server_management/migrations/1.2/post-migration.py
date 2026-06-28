"""1.1 → 1.2: the per-server Backup Project is replaced by a required erp/odex
category + a single global Space. Map each server's category from its old
project name, then drop the now-orphan column."""
import logging

_logger = logging.getLogger(__name__)


def migrate(cr, version):
    # Map backup_category from the old project (name containing 'erp' → erp).
    cr.execute("""
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'server_host' AND column_name = 'backup_project_id'
    """)
    if cr.fetchone():
        cr.execute("""
            UPDATE server_host h
               SET backup_category = CASE
                       WHEN lower(p.name) LIKE '%%erp%%' THEN 'erp'
                       ELSE 'odex' END
              FROM server_backup_project p
             WHERE h.backup_project_id = p.id
        """)
        # Any server without an old project still needs a non-null category.
        cr.execute("UPDATE server_host SET backup_category = 'odex' "
                   "WHERE backup_category IS NULL")
        cr.execute("ALTER TABLE server_host DROP COLUMN IF EXISTS backup_project_id")
        _logger.info("server_host.backup_category populated; dropped backup_project_id")

    # Drop the obsolete project table (model removed).
    cr.execute("DROP TABLE IF EXISTS server_backup_project CASCADE")

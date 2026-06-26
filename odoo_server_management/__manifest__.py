{
    'name': 'Odoo Server Management',
    'version': '1.1',
    'summary': 'Manage multiple Odoo servers with Ansible integration',
    'description': """
        This module allows centralized management of multiple Odoo servers using Ansible.
        Features include restarting, stopping, starting services, viewing logs, pulling code, and upgrading modules.
    """,
    'author': 'Moutaz Muhammad',
    'maintainer': 'Moutaz Muhammad',
    'website': 'https://github.com/moutazmuhammad',
    'category': 'Administration',
    'license': 'LGPL-3',
    'depends': ['base', 'auth_signup'],
    # Live logs stream via Server-Sent Events from the Odoo controller itself
    # (ssh `tail -f`), so no paramiko/websockets and no separate process are
    # needed — just the ssh client and ansible-playbook on the Odoo host.
    # boto3 is a SOFT dependency (per-project backups only) — imported lazily with
    # a clear error, so the module still loads on hosts without it.
    'external_dependencies': {
        'python': ['requests', 'yaml', 'cryptography'],
        'bin': ['ansible-playbook', 'ssh'],
    },
    'data': [
        'security/security.xml',
        'security/ir.model.access.csv',
        'data/ir_cron.xml',
        'views/menus.xml',
        'views/server_host_views.xml',
        'views/stage_views.xml',
        'views/pull_code.xml',
        'views/github_settings.xml',
        'views/backup_project_views.xml',
        'views/pull_code_wizard.xml',
        'views/view_conf_wizard.xml',
        'views/upgrade_module_wizard.xml',
        'views/log_stream_template.xml',
        'views/terminal_template.xml',
        'views/server_backup_database_wizard.xml',
    ],
    'installable': True,
    'application': True,
    'auto_install': False,
}


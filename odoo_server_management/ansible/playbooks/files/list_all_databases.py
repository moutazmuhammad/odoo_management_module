#!/usr/bin/env python3
"""List EVERY PostgreSQL database on this host (a plain DB server, no Odoo needed).

Connects as the `postgres` superuser over the local socket (peer auth) — the same
privilege the backup uses — via the pg_wrapper `psql`, which auto-targets the running
cluster whatever its port. Prints ODOO_ALLDBS_JSON:<base64 json list of db names>,
excluding templates and the built-in maintenance databases.
"""
import sys
import json
import base64
import shlex
import subprocess

SYS = {'postgres', 'template0', 'template1', 'defaultdb'}
QUERY = ("SELECT datname FROM pg_database "
         "WHERE NOT datistemplate AND datallowconn ORDER BY datname")


def sh(cmd):
    try:
        return subprocess.run(cmd, shell=True, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, universal_newlines=True,
                              timeout=30)
    except Exception:
        return None


def _have_sudo():
    r = sh("sudo -n true >/dev/null 2>&1 && echo 1")
    return bool(r and (r.stdout or '').strip() == '1')


q = shlex.quote(QUERY)
attempts = []
if _have_sudo():
    attempts.append("sudo -n -u postgres psql -w -tAc %s" % q)
attempts.append("psql -w -tAc %s -U postgres -d postgres" % q)
attempts.append("psql -w -tAc %s" % q)

dbs = []
for cmd in attempts:
    r = sh("PGCONNECT_TIMEOUT=5 " + cmd)
    if r and r.returncode == 0 and r.stdout.strip():
        dbs = [d.strip() for d in r.stdout.splitlines()
               if d.strip() and d.strip() not in SYS]
        break

print("ODOO_ALLDBS_JSON:" + base64.b64encode(json.dumps(dbs).encode()).decode())

#!/usr/bin/env python3
"""List the PostgreSQL databases for several Odoo instances on one host, in a
single SSH session.

argv[1] = base64(json) of [[key, conf_path, odoo_user], ...]
output  = ODOO_DBLIST_JSON:<base64 json {key: [db, ...]}>

Per instance the list is scoped the way Odoo itself scopes it: databases OWNED
by the conf's db_user; narrowed to db_name (single-db mode) or a literal
dbfilter when present.
"""
import sys, re, base64, json, subprocess, shlex


def conf_get(conf, key):
    try:
        for ln in open(conf, errors='ignore'):
            ln = ln.strip()
            if ln.startswith(key) and '=' in ln:
                return ln.split('=', 1)[1].strip()
    except Exception:
        pass
    return ''


def is_set(v):
    return v and v.lower() not in ('false', 'none', '')


def list_for(conf, odoo_user):
    db_user = conf_get(conf, 'db_user') or 'odoo'
    db_password = conf_get(conf, 'db_password')
    db_host = conf_get(conf, 'db_host')
    db_port = conf_get(conf, 'db_port')
    db_name = conf_get(conf, 'db_name')
    dbfilter = conf_get(conf, 'dbfilter')

    query = (
        "SELECT d.datname FROM pg_database d JOIN pg_roles r ON d.datdba = r.oid "
        "WHERE NOT d.datistemplate AND d.datallowconn AND r.rolname = '%s' "
        "ORDER BY d.datname" % db_user.replace("'", "''")
    )
    # -w: never prompt for a password (would hang with no tty); short connect
    # timeout so an unreachable/auth-failing attempt fails fast and we try the next.
    base = ['psql', '-w', '-U', shlex.quote(db_user), '-d', 'postgres', '-tA']
    if is_set(db_host):
        base += ['-h', shlex.quote(db_host)]
    if is_set(db_port):
        base += ['-p', shlex.quote(db_port)]
    psql = 'PGCONNECT_TIMEOUT=5 ' + ' '.join(base) + ' -c ' + shlex.quote(query)

    attempts = []
    if odoo_user:
        attempts.append('sudo -n -u %s bash -lc %s' % (shlex.quote(odoo_user), shlex.quote(psql)))
    if is_set(db_password):
        attempts.append('PGPASSWORD=%s %s' % (shlex.quote(db_password), psql))
    attempts.append(psql)

    out = ''
    for a in attempts:
        try:
            r = subprocess.run(a, shell=True, capture_output=True, text=True, timeout=12)
        except Exception:
            continue
        if r.returncode == 0 and r.stdout.strip():
            out = r.stdout.strip()
            break

    dbs = [l.strip() for l in out.splitlines() if l.strip()]
    if is_set(db_name):
        dbs = [db_name]
    elif dbfilter and '%' not in dbfilter:
        try:
            dbs = [d for d in dbs if re.match(dbfilter, d)]
        except re.error:
            pass
    return dbs


try:
    spec = json.loads(base64.b64decode(sys.argv[1]).decode())
except Exception:
    spec = []

result = {}
for item in spec:
    key = str(item[0])
    conf = item[1] if len(item) > 1 else ''
    user = item[2] if len(item) > 2 else ''
    if conf:
        result[key] = list_for(conf, user)

print("ODOO_DBLIST_JSON:" + base64.b64encode(json.dumps(result).encode()).decode())

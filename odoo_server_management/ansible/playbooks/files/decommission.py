#!/usr/bin/env python3
"""Decommission (permanently remove) ONE Odoo instance from a managed server.

argv[1] = base64(json) of {
  "service": "<systemd service name>",
  "conf": "<conf path>", "log": "<log path>", "nginx": "<nginx vhost path>",
  "databases": ["db1", ...],
  "remove_service": true, "remove_nginx": false,
  "drop_database": false, "remove_filestore": false
}
Prints ODOO_DECOMMISSION_JSON:<base64 json> describing exactly what was removed.

Self-escalates with passwordless sudo. It only ever touches THIS instance's own
systemd unit, conf, log, nginx vhost, database(s) and filestore — never any shared
source tree / addons / virtualenv, and only the paths the manager passed in.
"""
import os
import re
import sys
import json
import glob
import base64
import shlex
import subprocess


def sh(cmd):
    try:
        return subprocess.run(cmd, shell=True, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, universal_newlines=True,
                              timeout=180)
    except Exception:
        return None


_r = sh("sudo -n true >/dev/null 2>&1 && echo 1")
SUDO = 'sudo -n ' if (_r and (_r.stdout or '').strip() == '1') else ''


def run(cmd):
    """Run `cmd` (escalated). Returns (ok, stdout, stderr)."""
    r = sh(SUDO + cmd)
    if not r:
        return False, '', 'timeout/exec error'
    return r.returncode == 0, r.stdout or '', r.stderr or ''


def _lit(s):
    """A safe single-quoted SQL string literal."""
    return "'" + str(s).replace("'", "''") + "'"


def _data_dir_from_conf(conf):
    try:
        with open(conf, encoding='utf-8', errors='replace') as fh:
            for line in fh:
                m = re.match(r'\s*data_dir\s*=\s*(\S+)', line)
                if m:
                    return m.group(1).strip()
    except Exception:
        pass
    return ''


def _filestore_dirs(db, conf):
    """Candidate filestore directories for `db` (never deletes anything itself)."""
    bases = []
    dd = _data_dir_from_conf(conf) if conf else ''
    if dd:
        bases.append(dd)
    bases += ['/opt/odoo/data', '/var/lib/odoo',
              '/home/odoo/.local/share/Odoo', '/opt/odoo/.local/share/Odoo',
              os.path.expanduser('~/.local/share/Odoo')]
    out, seen = [], set()
    for b in bases:
        p = os.path.join(b, 'filestore', db)
        if p not in seen:
            seen.add(p)
            out.append(p)
    return out


try:
    spec = json.loads(base64.b64decode(sys.argv[1]).decode())
except Exception:
    spec = {}

done = {'service': None, 'files': [], 'nginx': None, 'databases': {},
        'filestore': [], 'errors': []}

svc = (spec.get('service') or '').strip()

# 1. The service itself: stop, disable, delete its unit file, conf and log.
if spec.get('remove_service') and svc:
    q = shlex.quote(svc)
    sh(SUDO + 'systemctl stop %s' % q)
    sh(SUDO + 'systemctl disable %s' % q)
    fp = sh(SUDO + 'systemctl show %s -p FragmentPath --value' % q)
    frag = (fp.stdout.strip() if fp and fp.stdout else '')
    unit_dirs = ('/etc/systemd/system', '/lib/systemd/system',
                 '/usr/lib/systemd/system')
    removed = []
    candidates = [frag,
                  '/etc/systemd/system/%s.service' % svc,
                  '/etc/systemd/system/%s' % svc]
    for path in candidates:
        if (path and path.endswith('.service')
                and os.path.dirname(path) in unit_dirs):
            ok, _o, _e = run('rm -f %s' % shlex.quote(path))
            if ok and path not in removed:
                removed.append(path)
    # drop-in overrides dir, if any
    run('rm -rf %s' % shlex.quote('/etc/systemd/system/%s.service.d' % svc))
    sh(SUDO + 'systemctl daemon-reload')
    sh(SUDO + 'systemctl reset-failed %s' % q)
    done['service'] = 'stopped, disabled and unit removed'
    for p in (spec.get('conf'), spec.get('log')):
        p = (p or '').strip()
        if p and os.path.isabs(p) and os.path.exists(p):
            ok, _o, _e = run('rm -f %s' % shlex.quote(p))
            if ok:
                removed.append(p)
    done['files'] = removed

# 2. nginx vhost (+ its sites-enabled symlink), then validate & reload.
if spec.get('remove_nginx'):
    ng = (spec.get('nginx') or '').strip()
    if ng and os.path.isabs(ng) and '/nginx/' in ng:
        ok, _o, _e = run('rm -f %s' % shlex.quote(ng))
        run('rm -f %s' % shlex.quote('/etc/nginx/sites-enabled/%s'
                                     % os.path.basename(ng)))
        if ok:
            done['nginx'] = ng
            t = sh(SUDO + 'nginx -t')
            if t and t.returncode == 0:
                sh(SUDO + 'systemctl reload nginx')
            else:
                done['errors'].append('nginx -t failed after removing the vhost; '
                                      'NOT reloaded — check nginx config by hand')

# 3. database(s): terminate connections, then DROP. (postgres peer auth.)
if spec.get('drop_database'):
    for db in (spec.get('databases') or []):
        db = (db or '').strip()
        if not db:
            continue
        term = ("SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE "
                "datname = %s AND pid <> pg_backend_pid();" % _lit(db))
        run('-u postgres psql -v ON_ERROR_STOP=0 -Atc %s' % shlex.quote(term))
        okd, _o, err = run('-u postgres psql -v ON_ERROR_STOP=1 -Atc %s'
                           % shlex.quote('DROP DATABASE IF EXISTS "%s";'
                                         % db.replace('"', '')))
        done['databases'][db] = 'dropped' if okd else ('error: %s' % (err or '').strip()[:200])
        # 4. filestore for that db.
        if spec.get('remove_filestore'):
            for d in _filestore_dirs(db, spec.get('conf')):
                if os.path.isdir(d):
                    okf, _o, _e = run('rm -rf %s' % shlex.quote(d))
                    if okf:
                        done['filestore'].append(d)

print('ODOO_DECOMMISSION_JSON:'
      + base64.b64encode(json.dumps(done).encode()).decode())

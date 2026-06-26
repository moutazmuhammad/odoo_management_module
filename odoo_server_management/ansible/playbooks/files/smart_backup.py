#!/usr/bin/env python3
"""Smart, self-detecting Odoo backup helper (runs ON a managed server as root,
DB ops via the postgres superuser). No cloud credentials are ever used here;
upload targets are short-lived pre-signed URLs supplied by Odoo.

Modes:
  detect
      Print ODOO_BACKUP_DETECT:<base64 json> — a list of
      {db, domain, filestore, size} for every Odoo database on this host. DBs are
      discovered live (psql) so new services/DBs are picked up automatically;
      `domain` comes from each DB's own web.base.url; `size` is a generous upper
      bound (pg_database_size + filestore bytes) used to pick the upload method.

  backup <mapfile>
      `mapfile` is JSON {db: target}. Each target is either
        {"mode":"single","url": "<presigned PUT>", "filestore": "..."}             or
        {"mode":"multipart","part_size":N,"part_urls":[...],"upload_id":"...","filestore":"..."}
      For each DB build an Odoo-IDENTICAL zip (dump.sql + filestore/ +
      manifest.json) and upload it. Everything streams (bounded memory/disk), so
      arbitrarily large databases work. Prints
      ODOO_BACKUP_RESULT:<base64 json> {db: {ok, ... , parts:[{PartNumber,ETag}]}}.
"""
import os
import re
import sys
import glob
import json
import shlex
import base64
import zipfile
import tempfile
import subprocess

SYSTEM_DBS = {'postgres', 'template0', 'template1'}
DETECT_MARKER = 'ODOO_BACKUP_DETECT:'
RESULT_MARKER = 'ODOO_BACKUP_RESULT:'


def _pg_prefix():
    """Run DB tools as the postgres superuser when possible (peer auth, can dump
    ANY database). Falls back to running them directly."""
    try:
        r = subprocess.run(['sudo', '-n', '-u', 'postgres', 'true'], capture_output=True)
        if r.returncode == 0:
            return ['sudo', '-n', '-u', 'postgres']
    except Exception:
        pass
    return []


PG = _pg_prefix()


def _run(cmd, **kw):
    return subprocess.run(cmd, capture_output=True, text=True, **kw)


def psql_scalar(db, sql):
    r = _run(PG + ['psql', '-d', db, '-tAc', sql])
    return r.stdout.strip() if r.returncode == 0 else ''


def list_databases():
    r = _run(PG + ['psql', '-tAc',
                   "SELECT datname FROM pg_database "
                   "WHERE NOT datistemplate AND datallowconn"])
    if r.returncode != 0:
        return []
    return [d.strip() for d in r.stdout.splitlines()
            if d.strip() and d.strip() not in SYSTEM_DBS]


def is_odoo_db(db):
    return psql_scalar(
        db, "SELECT 1 FROM information_schema.tables "
            "WHERE table_name='ir_module_module' LIMIT 1") == '1'


def _data_dir_candidates():
    dirs = set()
    for conf in (glob.glob('/etc/odoo*.conf') + glob.glob('/etc/odoo/*.conf')
                 + glob.glob('/opt/odoo/*.conf')):
        try:
            with open(conf) as fh:
                for line in fh:
                    m = re.match(r'\s*data_dir\s*=\s*(\S+)', line)
                    if m:
                        dirs.add(m.group(1))
        except OSError:
            pass
    for home in ('/opt/odoo', '/home/odoo', '/var/lib/odoo', os.path.expanduser('~')):
        dirs.add(os.path.join(home, '.local/share/Odoo'))
    dirs.add('/var/lib/odoo')
    return dirs


def find_filestore(db):
    for d in _data_dir_candidates():
        p = os.path.join(d, 'filestore', db)
        if os.path.isdir(p):
            return p
    return ''


def domain_for(db):
    val = psql_scalar(db, "SELECT value FROM ir_config_parameter WHERE key='web.base.url'")
    host = ''
    if val:
        m = re.match(r'^\s*https?://([^/:]+)', val.strip())
        host = m.group(1) if m else val.strip().split('/')[0]
    if re.match(r'^\d{1,3}(\.\d{1,3}){3}$', host):
        host = host.replace('.', '-')
    return host


def _dir_bytes(path):
    if not path or not os.path.isdir(path):
        return 0
    r = _run(['du', '-sb', path])
    try:
        return int(r.stdout.split()[0]) if r.returncode == 0 and r.stdout.split() else 0
    except (ValueError, IndexError):
        return 0


def _db_bytes(db):
    v = psql_scalar(db, "SELECT pg_database_size('%s')" % db.replace("'", "''"))
    try:
        return int(v)
    except (TypeError, ValueError):
        return 0


def detect():
    out = []
    for db in list_databases():
        if not is_odoo_db(db):
            continue
        fs = find_filestore(db)
        out.append({'db': db, 'domain': domain_for(db), 'filestore': fs,
                    'size': _db_bytes(db) + _dir_bytes(fs)})
    print(DETECT_MARKER + base64.b64encode(json.dumps(out).encode()).decode())


def build_manifest(db):
    base_ver = psql_scalar(
        db, "SELECT latest_version FROM ir_module_module WHERE name='base'") or '0.0.0.0'
    parts = base_ver.split('.')
    major = '.'.join(parts[:2]) if len(parts) >= 2 else base_ver
    pgv = psql_scalar(db, 'SHOW server_version')
    m = re.match(r'(\d+(?:\.\d+)?)', pgv or '')
    pgv = m.group(1) if m else pgv
    mods = {}
    r = _run(PG + ['psql', '-d', db, '-tAF', '\t', '-c',
                   "SELECT name, latest_version FROM ir_module_module "
                   "WHERE state='installed'"])
    for line in r.stdout.splitlines():
        if '\t' in line:
            n, v = line.split('\t', 1)
            mods[n.strip()] = v.strip()
    vmaj = int(parts[0]) if parts and parts[0].isdigit() else 0
    vmin = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 0
    return {
        'odoo_dump': '1', 'db_name': db, 'version': major,
        'version_info': [vmaj, vmin, 0, 'final', 0, ''],
        'major_version': major, 'pg_version': pgv, 'modules': mods,
    }


def build_zip(db, filestore, zip_path):
    """Build an Odoo-identical backup zip, streaming pg_dump to disk."""
    with tempfile.TemporaryDirectory() as td:
        dump = os.path.join(td, 'dump.sql')
        # pg_dump runs as postgres; redirect into a file WE own so postgres needs
        # no write access to our tempdir. Streams to disk (no RAM blow-up).
        cmd = ' '.join(PG + ['pg_dump', '--no-owner', '-d', shlex.quote(db)])
        with open(dump, 'wb') as fh:
            r = subprocess.run(cmd, shell=True, stdout=fh, stderr=subprocess.PIPE)
        if r.returncode != 0:
            raise RuntimeError('pg_dump failed: %s'
                               % (r.stderr or b'').decode('utf-8', 'replace').strip()[:300])
        with open(os.path.join(td, 'manifest.json'), 'w') as fh:
            json.dump(build_manifest(db), fh, indent=4)
        with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED, allowZip64=True) as z:
            z.write(dump, 'dump.sql')
            z.write(os.path.join(td, 'manifest.json'), 'manifest.json')
            if filestore and os.path.isdir(filestore):
                for root, _dirs, files in os.walk(filestore):
                    for f in files:
                        full = os.path.join(root, f)
                        rel = os.path.relpath(full, filestore)
                        z.write(full, os.path.join('filestore', rel))


def _curl_put(url, path, length=None, offset=0):
    """PUT bytes [offset:offset+length] of `path` to `url`; return the ETag.
    Streams via a shell pipe (dd | curl) so no part is loaded into memory."""
    if length is None:
        cmd = ['curl', '-sS', '--fail', '-X', 'PUT', '-D', '-', '-o', '/dev/null',
               '--upload-file', path, url]
        r = _run(cmd)
        head = r.stdout
    else:
        # dd extracts an exact byte range; curl streams it with a known length.
        pipe = ('dd if=%s bs=8M skip=%d count=%d iflag=skip_bytes,count_bytes '
                '2>/dev/null | curl -sS --fail -X PUT -D - -o /dev/null '
                '-H %s --data-binary @- %s'
                % (shlex.quote(path), offset, length,
                   shlex.quote('Content-Length: %d' % length), shlex.quote(url)))
        r = subprocess.run(pipe, shell=True, capture_output=True, text=True)
        head = r.stdout
    if r.returncode != 0:
        raise RuntimeError('upload failed (%s): %s'
                           % (r.returncode, (r.stderr or '').strip()[:200]))
    m = re.search(r'(?i)ETag:\s*"?([^"\r\n]+)"?', head or '')
    return (m.group(1) if m else '').strip()


SINGLE_LIMIT = 4 * 1024 ** 3  # 4 GiB — stay safely under the 5 GB single-PUT cap


def _upload_zip(zip_path, target):
    size = os.path.getsize(zip_path)
    mode = target.get('mode', 'single')
    # Force multipart if the file turned out larger than a single PUT allows.
    if mode == 'single' and size > SINGLE_LIMIT and target.get('part_urls'):
        mode = 'multipart'
    if mode != 'multipart':
        _curl_put(target['url'], zip_path)
        return {'ok': True, 'mode': 'single', 'bytes': size}

    part_size = int(target['part_size'])
    urls = target['part_urls']
    parts = []
    idx = 0
    offset = 0
    while offset < size:
        if idx >= len(urls):
            raise RuntimeError('not enough pre-signed parts for %d bytes' % size)
        length = min(part_size, size - offset)
        etag = _curl_put(urls[idx], zip_path, length=length, offset=offset)
        parts.append({'PartNumber': idx + 1, 'ETag': etag})
        offset += length
        idx += 1
    return {'ok': True, 'mode': 'multipart', 'bytes': size,
            'upload_id': target.get('upload_id'), 'parts': parts}


def backup(mapfile):
    with open(mapfile) as fh:
        targets = json.load(fh)
    results = {}
    for db, target in targets.items():
        if not isinstance(target, dict):
            target = {'mode': 'single', 'url': target}
        fs = target.get('filestore') or find_filestore(db)
        try:
            with tempfile.TemporaryDirectory() as td:
                zp = os.path.join(td, db + '.zip')
                build_zip(db, fs, zp)
                res = _upload_zip(zp, target)
                res['upload_id'] = res.get('upload_id') or target.get('upload_id')
                results[db] = res
        except Exception as exc:  # noqa: BLE001
            results[db] = {'ok': False, 'error': str(exc),
                           'mode': target.get('mode'),
                           'upload_id': target.get('upload_id')}
    print(RESULT_MARKER + base64.b64encode(json.dumps(results).encode()).decode())


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else 'detect'
    if mode == 'detect':
        detect()
    elif mode == 'backup' and len(sys.argv) > 2:
        backup(sys.argv[2])
    else:
        sys.stderr.write('usage: smart_backup.py detect | backup <mapfile>\n')
        sys.exit(2)


if __name__ == '__main__':
    main()

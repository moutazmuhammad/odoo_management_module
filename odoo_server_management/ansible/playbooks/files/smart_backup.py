#!/usr/bin/env python3
"""Smart, self-detecting Odoo backup helper (runs ON a managed/client server as
root). No cloud credentials are ever used here; upload targets are short-lived
pre-signed URLs supplied by the manager. Everything (dump, zip, upload) happens
on THIS server.

Works with BOTH database topologies:
  * Local PostgreSQL (peer auth via the `postgres` OS user / Unix socket).
  * Remote / managed PostgreSQL — connection params (db_host, db_port, db_user,
    db_password, db_name, dbfilter) are read from the Odoo .conf files on this
    host, and psql/pg_dump connect over TCP with PGPASSWORD.

Modes:
  detect
      Print ODOO_BACKUP_DETECT:<base64 json> — a list of
      {db, domain, filestore, size} for every Odoo database reachable from this
      host. `size` is a generous upper bound used to pick single vs multipart.

  backup <mapfile>
      `mapfile` is JSON {db: target}. Each target is either
        {"mode":"single","url":"<presigned PUT>","filestore":"..."}            or
        {"mode":"multipart","part_size":N,"part_urls":[...],"upload_id":"...","filestore":"..."}
      For each DB build an Odoo-IDENTICAL zip (dump.sql + filestore/ +
      manifest.json) and upload it. pg_dump is streamed straight into the zip
      entry and the zip lives on a disk-backed tmp dir, so 30 GB+ databases work
      with bounded memory and minimal peak disk. Prints
      ODOO_BACKUP_RESULT:<base64 json> {db: {ok, ..., parts:[{PartNumber,ETag}]}}.
"""
import os
import re
import sys
import time
import glob
import json
import shlex
import shutil
import base64
import zipfile
import tempfile
import subprocess

SYSTEM_DBS = {'postgres', 'template0', 'template1', 'defaultdb'}
DETECT_MARKER = 'ODOO_BACKUP_DETECT:'
RESULT_MARKER = 'ODOO_BACKUP_RESULT:'
SINGLE_LIMIT = 4 * 1024 ** 3  # 4 GiB — stay safely under the 5 GB single-PUT cap
CHUNK = 8 * 1024 * 1024


def _pg_prefix():
    """Run local DB tools as the postgres superuser when possible (peer auth)."""
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


def _scan_pg_dumps():
    """All installed pg_dump binaries keyed by major version."""
    bins = {}
    for p in glob.glob('/usr/lib/postgresql/*/bin/pg_dump'):
        m = re.search(r'/postgresql/(\d+)/bin/', p)
        if m:
            bins[int(m.group(1))] = p
    try:
        r = subprocess.run(['pg_dump', '--version'], capture_output=True, text=True)
        mm = re.search(r'(\d+)\.\d+', r.stdout or '')
        if mm:
            bins.setdefault(int(mm.group(1)), 'pg_dump')
    except Exception:
        pass
    return bins


def _install_pg_client(major):
    """Best-effort install of postgresql-client-<major> (PGDG repo) so we can dump a
    newer server. Needs passwordless sudo + the repo; failures are ignored (the
    caller then raises a clear error)."""
    for cmd in (['sudo', '-n', 'apt-get', 'update', '-qq'],
                ['sudo', '-n', 'apt-get', 'install', '-y', '-qq',
                 'postgresql-client-%d' % major]):
        try:
            subprocess.run(cmd, capture_output=True, text=True, timeout=600)
        except Exception:
            return


def _pg_dump_bin(server_major):
    """Pick a pg_dump whose major >= the SERVER's (pg_dump refuses to dump from a
    newer server). If none is installed, try to install the matching client and
    re-scan; raise a clear, actionable error if it still can't be satisfied
    (instead of the cryptic 'server version mismatch')."""
    def _pick(bins):
        for v in sorted(bins):
            if v >= (server_major or 0):
                return bins[v]
        return None

    bins = _scan_pg_dumps()
    chosen = _pick(bins)
    if chosen:
        return chosen
    if server_major:  # self-heal: install the matching client, then retry
        _install_pg_client(server_major)
        bins = _scan_pg_dumps()
        chosen = _pick(bins)
        if chosen:
            return chosen
    if bins:
        raise RuntimeError(
            "No pg_dump >= server major %s on this host (installed: %s). Install "
            "'postgresql-client-%s'." % (server_major, sorted(bins), server_major))
    return 'pg_dump'


# ======================================================================
# Database connection (local peer OR remote TCP), derived from Odoo confs
# ======================================================================
class Conn:
    """A way to reach one PostgreSQL server. Local = peer auth via `postgres`;
    remote = TCP with -h/-p/-U and PGPASSWORD."""

    def __init__(self, host='', port='', user='', password=''):
        h = (host or '').strip()
        self.local = h in ('', 'localhost', '127.0.0.1', '::1',
                            '/var/run/postgresql', '/tmp', 'false', 'False')
        self.host = '' if self.local else h
        self.port = str(port or '').strip()
        self.user = (user or '').strip()
        self.password = password or ''

    def key(self):
        return 'local' if self.local else '%s:%s:%s' % (self.host, self.port, self.user)

    def _cmd(self, tool, db=None, extra=None):
        env = dict(os.environ)
        env['LC_ALL'] = 'C'          # avoid locale warnings corrupting stdout
        if self.local:
            cmd = PG + [tool]
        else:
            cmd = [tool, '-h', self.host]
            if self.port:
                cmd += ['-p', self.port]
            if self.user:
                cmd += ['-U', self.user]
            if self.password:
                env['PGPASSWORD'] = self.password
        if db:
            cmd += ['-d', db]
        if extra:
            cmd += list(extra)
        return cmd, env

    def psql_scalar(self, db, sql):
        cmd, env = self._cmd('psql', db, ['-tAc', sql])
        r = subprocess.run(cmd, capture_output=True, text=True, env=env)
        return r.stdout.strip() if r.returncode == 0 else ''

    def server_major(self, db):
        sv = self.psql_scalar(db, 'SHOW server_version_num')
        try:
            return int(sv) // 10000
        except (TypeError, ValueError):
            m = re.match(r'(\d+)', self.psql_scalar(db, 'SHOW server_version') or '')
            return int(m.group(1)) if m else 0

    def dump_cmd(self, db, server_major, extra=None):
        """pg_dump command using a binary that matches the server major version."""
        binpath = _pg_dump_bin(server_major)
        env = dict(os.environ)
        env['LC_ALL'] = 'C'
        if self.local:
            cmd = PG + [binpath]
        else:
            cmd = [binpath, '-h', self.host]
            if self.port:
                cmd += ['-p', self.port]
            if self.user:
                cmd += ['-U', self.user]
            if self.password:
                env['PGPASSWORD'] = self.password
        cmd += ['-d', db]
        if extra:
            cmd += list(extra)
        return cmd, env

    def _maint_candidates(self, hints=()):
        out, seen = [], set()
        for c in list(hints) + ['defaultdb', self.user, 'postgres']:
            if c and c not in seen:
                seen.add(c)
                out.append(c)
        return out

    def query_list(self, sql, hints=()):
        """Run `sql` against a maintenance DB and return the first column as a
        list. Local uses peer auth; remote tries connectable maintenance DBs."""
        cands = [None, 'postgres'] if self.local else self._maint_candidates(hints)
        for cand in cands:
            cmd, env = self._cmd('psql', cand, ['-tAc', sql])
            r = subprocess.run(cmd, capture_output=True, text=True, env=env)
            if r.returncode == 0:
                return [d.strip() for d in r.stdout.splitlines() if d.strip()]
        return []

    def list_databases(self, hints=()):
        return self.query_list(
            "SELECT datname FROM pg_database WHERE NOT datistemplate AND datallowconn",
            hints=hints)


def _parse_conf(path):
    cfg = {}
    try:
        with open(path) as fh:
            for line in fh:
                s = line.strip()
                if not s or s.startswith((';', '#')):
                    continue
                m = re.match(r'([A-Za-z0-9_]+)\s*=\s*(.*)$', s)
                if m:
                    cfg[m.group(1).lower()] = m.group(2).strip()
    except OSError:
        return None
    return cfg


def _conf_files():
    confs = set()
    try:
        ps = subprocess.run(['ps', '-eo', 'args'], capture_output=True, text=True).stdout
    except Exception:
        ps = ''
    for line in ps.splitlines():
        if 'odoo' not in line.lower() and 'openerp' not in line.lower():
            continue
        for mm in re.finditer(r'(?:-c|--config)[ =]\s*(\S+)', line):
            confs.add(mm.group(1))
    for pat in ('/etc/odoo*.conf', '/etc/odoo/*.conf', '/etc/*odoo*/*.conf',
                '/opt/odoo*/*.conf', '/opt/odoo*/*/*.conf', '/opt/*/*.conf'):
        confs.update(glob.glob(pat))
    return {c for c in confs if os.path.isfile(c)}


def _sources():
    """Map every distinct DB server reachable from this host to its confs.
    Always includes a local-socket source (covers hosts with a local Postgres
    and no explicit db_host)."""
    srcs = {}
    for path in _conf_files():
        cfg = _parse_conf(path)
        if cfg is None:
            continue
        conn = Conn(cfg.get('db_host', ''), cfg.get('db_port', ''),
                    cfg.get('db_user', ''), cfg.get('db_password', ''))
        s = srcs.setdefault(conn.key(), {'conn': conn, 'confs': []})
        s['confs'].append(cfg)
    local = Conn()
    srcs.setdefault(local.key(), {'conn': local, 'confs': []})
    return srcs


def _is_set(v):
    return v and v.strip().lower() not in ('false', 'none', '')


def _owned_dbs(conn, db_user, db_name, dbfilter):
    """Databases for ONE Odoo instance, scoped exactly the way the module's
    'list databases' (Upgrade Module button) does: only DBs OWNED by the conf's
    db_user, narrowed to db_name (single-db mode) or a literal dbfilter."""
    if _is_set(db_name):
        return [db_name.strip()]
    if not db_user:
        return []
    sql = (
        "SELECT d.datname FROM pg_database d JOIN pg_roles r ON d.datdba = r.oid "
        "WHERE NOT d.datistemplate AND d.datallowconn AND r.rolname = '%s' "
        "ORDER BY d.datname" % db_user.replace("'", "''"))
    dbs = [d for d in conn.query_list(sql) if d and d not in SYSTEM_DBS]
    # Apply a LITERAL dbfilter (skip patterns containing % placeholders), exactly
    # like list_databases.py.
    if dbfilter and '%' not in dbfilter:
        try:
            dbs = [d for d in dbs if re.match(dbfilter, d)]
        except re.error:
            pass
    return dbs


def _dbfilter_stem(dbfilter):
    """Literal base name of a dbfilter, i.e. the canonical/served DB name.
    'takafol_dev_db.*$' -> 'takafol_dev_db'. Returns '' if the filter has %
    placeholders or still contains regex metacharacters after stripping."""
    f = (dbfilter or '').strip()
    if not f or '%' in f:
        return ''
    if f.startswith('^'):
        f = f[1:]
    if f.endswith('$'):
        f = f[:-1]
    if f.endswith('.*'):
        f = f[:-2]
    if re.search(r'[.*+?\[\]()|\\^$]', f):
        return ''
    return f


def _exposed_db(conn, cfg):
    """The single EXPOSED/served database for one instance (conf). Resolution:
      1) explicit db_name, else
      2) the DB whose name == the dbfilter stem (the canonical name), else
      3) the unique DB matching the filter.
    Returns (db_or_None, ambiguous_candidates). Old/dated copies are excluded
    because they don't equal the stem and aren't the unique match."""
    db_name = cfg.get('db_name', '')
    if _is_set(db_name):
        return db_name.strip(), []
    db_user = cfg.get('db_user', '') or ('odoo' if conn.local else '')
    dbfilter = cfg.get('dbfilter', '')
    owned = _owned_dbs(conn, db_user, '', dbfilter)
    if not owned:
        return None, []
    stem = _dbfilter_stem(dbfilter)
    if stem and stem in owned:
        return stem, []
    if len(owned) == 1:
        return owned[0], []
    return None, owned


# ======================================================================
# Per-DB metadata (Odoo-ness, domain, filestore, size)
# ======================================================================
def is_odoo_db(conn, db):
    return conn.psql_scalar(
        db, "SELECT 1 FROM information_schema.tables "
            "WHERE table_name='ir_module_module' LIMIT 1") == '1'


def _nginx_blocks(text, keyword):
    """Yield the inner text of each top-level `keyword { ... }` block (brace-matched)."""
    blocks, i = [], 0
    while True:
        idx = text.find(keyword, i)
        if idx == -1:
            break
        brace = text.find('{', idx)
        if brace == -1:
            break
        depth, j = 0, brace
        while j < len(text):
            if text[j] == '{':
                depth += 1
            elif text[j] == '}':
                depth -= 1
                if depth == 0:
                    break
            j += 1
        blocks.append(text[brace + 1:j])
        i = j + 1
    return blocks


def parse_nginx():
    """Map a proxied odoo port -> {'domain', 'listen', 'file'} from the nginx site
    configs. The odoo instance is matched to its nginx vhost BY PORT (nginx
    proxy_pass port == the instance's http_port). 'domain' is the primary
    server_name (empty if the vhost has none), 'listen' is the vhost's public
    listen port, 'file' is the site-config path."""
    dirs = ['/etc/nginx/sites-enabled', '/etc/nginx/sites-enable', '/etc/nginx/conf.d']
    texts = {}
    for d in dirs:
        if not os.path.isdir(d):
            continue
        try:
            names = sorted(os.listdir(d))
        except Exception:
            continue
        for fn in names:
            p = os.path.join(d, fn)
            try:
                if os.path.isfile(p):
                    texts[p] = open(p, errors='ignore').read()
            except Exception:
                pass
    upstreams = {}
    for text in texts.values():
        for m in re.finditer(r'upstream\s+(\S+)\s*\{([^}]*)\}', text, re.S):
            upstreams[m.group(1)] = re.findall(r'server\s+[^;]*?:(\d+)', m.group(2))
    port_info = {}
    for path, text in texts.items():
        for block in _nginx_blocks(text, 'server'):
            domains = []
            for sn in re.findall(r'server_name\s+([^;]+);', block):
                for tok in sn.split():
                    tok = tok.strip()
                    if tok and tok not in ('_', 'localhost') and not tok.startswith('$'):
                        domains.append(tok)
            primary = domains[0] if domains else ''
            lm = re.search(r'listen\s+([^;]+);', block)
            listen = ''
            if lm:
                pm = re.search(r'(\d+)\s*$', lm.group(1).split()[0])
                listen = pm.group(1) if pm else ''
            ports = re.findall(
                r'proxy_pass\s+https?://(?:\d{1,3}(?:\.\d{1,3}){3}|localhost|'
                r'127\.0\.0\.1):(\d+)', block)
            for up in re.findall(r'proxy_pass\s+https?://([A-Za-z_][\w.-]*)', block):
                ports.extend(upstreams.get(up, []))
            for prt in ports:
                cur = port_info.get(prt)
                # Prefer a vhost that actually has a domain over a domainless one.
                if cur is None or (not cur.get('domain') and primary):
                    port_info[prt] = {'domain': primary, 'listen': listen, 'file': path}
    return port_info


def _data_dir_candidates():
    dirs, confs = set(), set()
    try:
        ps = subprocess.run(['ps', '-eo', 'args'], capture_output=True, text=True).stdout
    except Exception:
        ps = ''
    for line in ps.splitlines():
        if 'odoo' not in line.lower() and 'openerp' not in line.lower():
            continue
        for mm in re.finditer(r'(?:-c|--config)[ =]\s*(\S+)', line):
            confs.add(mm.group(1))
        for mm in re.finditer(r'(?:-D|--data-dir)[ =]\s*(\S+)', line):
            dirs.add(mm.group(1))
    confs.update(_conf_files())
    for conf in confs:
        cfg = _parse_conf(conf) or {}
        if cfg.get('data_dir'):
            dirs.add(cfg['data_dir'])
    for home in ('/opt/odoo', '/home/odoo', '/var/lib/odoo', os.path.expanduser('~')):
        dirs.add(os.path.join(home, '.local/share/Odoo'))
    dirs.add('/var/lib/odoo')
    return {d for d in dirs if d}


def find_filestore(db):
    for d in _data_dir_candidates():
        p = os.path.join(d, 'filestore', db)
        if os.path.isdir(p):
            return p
    try:
        r = subprocess.run(
            ['find', '/opt', '/home', '/var/lib', '-maxdepth', '7', '-type', 'd',
             '-path', '*/filestore/' + db, '-print', '-quit'],
            capture_output=True, text=True, timeout=90)
        for hit in r.stdout.splitlines():
            if hit and os.path.isdir(hit):
                return hit
    except Exception:
        pass
    return ''


def _dir_bytes(path):
    if not path or not os.path.isdir(path):
        return 0
    r = _run(['du', '-sb', path])
    try:
        return int(r.stdout.split()[0]) if r.returncode == 0 and r.stdout.split() else 0
    except (ValueError, IndexError):
        return 0


def _db_bytes(conn, db):
    v = conn.psql_scalar(db, "SELECT pg_database_size('%s')" % db.replace("'", "''"))
    try:
        return int(v)
    except (TypeError, ValueError):
        return 0


def _append_db(out, seen, conn, db, nginx=None, http_port=''):
    if not db or db in seen or not is_odoo_db(conn, db):
        return
    seen.add(db)
    fs = find_filestore(db)
    # The backup path segment comes from the instance's nginx vhost (matched by the
    # odoo http_port). Domain if the vhost has one; else the public ip:port (the
    # manager fills the ip) — nginx listen port if known, else the conf http_port.
    info = ((nginx or {}).get(str(http_port).strip()) if http_port else None) or {}
    domain = info.get('domain') or ''
    port = '' if domain else (info.get('listen')
                              or (str(http_port).strip() if http_port else ''))
    out.append({'db': db, 'domain': domain, 'port': port,
                'nginx_file': info.get('file') or '',
                'filestore': fs, 'size': _db_bytes(conn, db) + _dir_bytes(fs)})


def detect_items(force_dbs=()):
    """Return (items, skipped). `items` = one entry per EXPOSED database per stage:
    the single canonical DB each instance serves (db_name -> dbfilter stem ->
    unique match), scoped by db_user ownership the way the Upgrade Module button
    lists databases. `force_dbs` (the host override) are always included by name."""
    out, seen, skipped = [], set(), []
    nginx = parse_nginx()
    for path in _conf_files():
        cfg = _parse_conf(path)
        if cfg is None:
            continue
        conn = Conn(cfg.get('db_host', ''), cfg.get('db_port', ''),
                    cfg.get('db_user', ''), cfg.get('db_password', ''))
        db, ambiguous = _exposed_db(conn, cfg)
        if not db:
            if ambiguous:
                skipped.append({'conf': path, 'candidates': ambiguous})
            continue
        http_port = (cfg.get('http_port') or cfg.get('xmlrpc_port') or '').strip()
        _append_db(out, seen, conn, db, nginx, http_port)
    for db in force_dbs or []:
        db = (db or '').strip()
        if db and db not in seen:
            _append_db(out, seen, _resolve_conn(db), db, nginx)
    return out, skipped


def _size_targets(targets):
    """Given manager-provided targets [{db, domain, port}] (the authoritative list of
    every stage's databases + path segment), resolve each db's connection on THIS
    host and attach filestore + size. The manager already decided WHAT to back up and
    the segment; the client only adds what it knows. A db that can't be reached here
    is dropped (the manager then sees it missing and reports it, never a false OK)."""
    out, seen = [], set()
    for t in targets:
        db = (t.get('db') or '').strip()
        if not db or db in seen:
            continue
        seen.add(db)
        conn = _resolve_conn(db)
        # The manager already chose WHAT to back up (every stage's DBs). Include any
        # CONNECTABLE database here — a plain pg_dump works even if the DB has no
        # Odoo tables yet (e.g. a raw/non-initialised DB). Only a DB we genuinely
        # cannot reach is dropped (then the manager reports it).
        if not _db_connectable(conn, db):
            continue
        fs = find_filestore(db)
        out.append({'db': db, 'domain': t.get('domain') or '', 'port': t.get('port') or '',
                    'filestore': fs, 'size': _db_bytes(conn, db) + _dir_bytes(fs)})
    return out


def detect(arg=()):
    # Manager-driven: arg is a list of {db,domain,port} targets -> just size them.
    if arg and isinstance(arg[0], dict):
        out = _size_targets(arg)
        print(DETECT_MARKER + base64.b64encode(json.dumps(out).encode()).decode())
        return
    # Fallback: client-side auto-detect (arg = list of forced db names).
    out, skipped = detect_items(arg)
    print(DETECT_MARKER + base64.b64encode(json.dumps(out).encode()).decode())
    # Diagnostic only (ignored by the parser): instances we could not resolve to
    # a single DB, so an operator can set db_name / tighten dbfilter.
    if skipped:
        print('ODOO_BACKUP_SKIPPED:'
              + base64.b64encode(json.dumps(skipped).encode()).decode())


def _db_connectable(conn, db):
    """True if we can open `db` over this connection (whether or not it is an Odoo
    DB) — used so raw/non-initialised DBs are still backed up via pg_dump."""
    return conn.psql_scalar(db, 'SELECT 1') == '1'


def _resolve_conn(db):
    """Find the connection that can actually reach `db` (remote first). Prefer a
    connection where it is an Odoo DB, but fall back to any that can simply open it
    so non-Odoo DBs are still dumpable."""
    srcs = list(_sources().values())
    ordered = ([x for x in srcs if not x['conn'].local]
               + [x for x in srcs if x['conn'].local])
    for s in ordered:
        if is_odoo_db(s['conn'], db):
            return s['conn']
    for s in ordered:
        if _db_connectable(s['conn'], db):
            return s['conn']
    return Conn()


# ======================================================================
# Build + upload
# ======================================================================
def build_manifest(conn, db):
    base_ver = conn.psql_scalar(
        db, "SELECT latest_version FROM ir_module_module WHERE name='base'") or '0.0.0.0'
    parts = base_ver.split('.')
    major = '.'.join(parts[:2]) if len(parts) >= 2 else base_ver
    pgv = conn.psql_scalar(db, 'SHOW server_version')
    m = re.match(r'(\d+(?:\.\d+)?)', pgv or '')
    pgv = m.group(1) if m else pgv
    mods = {}
    cmd, env = conn._cmd('psql', db, ['-tAF', '\t', '-c',
                         "SELECT name, latest_version FROM ir_module_module "
                         "WHERE state='installed'"])
    r = subprocess.run(cmd, capture_output=True, text=True, env=env)
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


def _write_zip_contents(z, conn, db, filestore):
    """Write the Odoo-identical entries (dump.sql + manifest.json + filestore/)
    into an open ZipFile `z`. pg_dump is streamed straight into the entry, so the
    database is never staged uncompressed on disk."""
    cmd, env = conn.dump_cmd(db, conn.server_major(db), ['--no-owner'])
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, env=env)
    # force_zip64: the dump is streamed with an unknown size into a possibly
    # non-seekable target (multipart), so ZIP64 must be reserved up front —
    # otherwise a >4 GB dump.sql raises "File size exceeded ZIP64 limit".
    with z.open('dump.sql', 'w', force_zip64=True) as zf:
        while True:
            chunk = proc.stdout.read(CHUNK)
            if not chunk:
                break
            zf.write(chunk)
    err = proc.stderr.read()
    if proc.wait() != 0:
        raise RuntimeError('pg_dump failed: %s'
                           % (err or b'').decode('utf-8', 'replace').strip()[:300])
    with z.open('manifest.json', 'w') as zf:
        zf.write(json.dumps(build_manifest(conn, db), indent=4).encode())
    if filestore and os.path.isdir(filestore):
        for root, _dirs, files in os.walk(filestore):
            for f in files:
                full = os.path.join(root, f)
                rel = os.path.relpath(full, filestore)
                z.write(full, os.path.join('filestore', rel))


def build_zip(conn, db, filestore, zip_path):
    """Build the backup zip to a file (used for single-PUT uploads)."""
    with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED, allowZip64=True) as z:
        _write_zip_contents(z, conn, db, filestore)


class _StreamingMultipart:
    """A write-only, non-seekable file object that ZipFile writes into. It buffers
    up to `part_size` bytes on disk, then uploads that part to the next pre-signed
    URL and deletes it — so peak scratch disk is ONE part (~512 MiB) no matter how
    big the database is (50 GB, 500 GB, …). The zip is never fully staged."""

    def __init__(self, part_urls, part_size, tmp_base):
        self.part_urls = part_urls
        self.part_size = int(part_size)
        self.tmp_base = tmp_base
        self.parts = []
        self.total = 0
        self.idx = 0
        self._path = None
        self._fh = None
        self._len = 0
        self._open()

    def _open(self):
        fd, self._path = tempfile.mkstemp(dir=self.tmp_base, suffix='.part')
        self._fh = os.fdopen(fd, 'wb')
        self._len = 0

    def write(self, data):
        mv = memoryview(data)
        n = len(mv)
        while len(mv):
            room = self.part_size - self._len
            take = mv[:room]
            self._fh.write(take)
            self._len += len(take)
            self.total += len(take)
            mv = mv[len(take):]
            if self._len >= self.part_size:
                self._flush(final=False)
        return n

    def _flush(self, final):
        self._fh.close()
        if self._len > 0:
            if self.idx >= len(self.part_urls):
                raise RuntimeError('not enough pre-signed parts')
            etag = _curl_put(self.part_urls[self.idx], self._path)
            self.parts.append({'PartNumber': self.idx + 1, 'ETag': etag})
            self.idx += 1
        try:
            os.remove(self._path)
        except OSError:
            pass
        if not final:
            self._open()

    def tell(self):
        return self.total

    def seekable(self):
        return False

    def writable(self):
        return True

    def flush(self):
        pass

    def close(self):
        if self._fh and not self._fh.closed:
            self._flush(final=True)

    def abort(self):
        try:
            if self._fh and not self._fh.closed:
                self._fh.close()
            if self._path and os.path.exists(self._path):
                os.remove(self._path)
        except OSError:
            pass


def _curl_put(url, path, length=None, offset=0):
    if length is None:
        cmd = ['curl', '-sS', '--fail', '-X', 'PUT', '-D', '-', '-o', '/dev/null',
               '--upload-file', path, url]
        r = _run(cmd)
        head = r.stdout
    else:
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


def _upload_zip(zip_path, target):
    size = os.path.getsize(zip_path)
    mode = target.get('mode', 'single')
    if mode == 'single' and size > SINGLE_LIMIT and target.get('part_urls'):
        mode = 'multipart'
    if mode != 'multipart':
        _curl_put(target['url'], zip_path)
        return {'ok': True, 'mode': 'single', 'bytes': size}
    part_size = int(target['part_size'])
    urls = target['part_urls']
    parts, idx, offset = [], 0, 0
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


WORKDIR_NAME = 'odoo-backup-work'


def _sweep_stale(workdir, max_age_s=3600):
    """Delete leftovers from a PREVIOUS run that was killed (SIGKILL / timeout)
    before its TemporaryDirectory / multipart-abort cleanup could run. Without
    this, a killed run leaves a half-built zip or part on disk and backups would
    accumulate on the server — the one place they must never be stored. Only
    touches items older than `max_age_s` so a concurrent run is never disturbed."""
    now = time.time()
    for name in os.listdir(workdir):
        p = os.path.join(workdir, name)
        try:
            if now - os.path.getmtime(p) < max_age_s:
                continue
            if os.path.isdir(p):
                shutil.rmtree(p, ignore_errors=True)
            else:
                os.remove(p)
        except OSError:
            pass


def _tmp_base():
    """A disk-backed, self-cleaning work dir with room for big zips (avoid /tmp,
    often a small tmpfs/RAM mount). Everything a run writes goes UNDER this dir
    and is deleted after upload; any stale leftovers from a killed run are swept
    here so no backup is ever left stored on the server."""
    for d in (os.environ.get('ODOO_BACKUP_TMPDIR'), '/var/tmp', tempfile.gettempdir()):
        if d and os.path.isdir(d) and os.access(d, os.W_OK):
            work = os.path.join(d, WORKDIR_NAME)
            try:
                os.makedirs(work, exist_ok=True)
                _sweep_stale(work)
            except OSError:
                continue
            return work
    return None


def run_targets(targets):
    """Build + upload each DB given a {db: target} map of pre-signed URLs. Returns
    a {db: result} map. Used both by the manager-driven flow and the standalone
    agent."""
    results = {}
    base = _tmp_base()
    for db, target in targets.items():
        if not isinstance(target, dict):
            target = {'mode': 'single', 'url': target}
        try:
            conn = _resolve_conn(db)
            fs = target.get('filestore') or find_filestore(db)
            if target.get('mode') == 'multipart' and target.get('part_urls'):
                # Stream the zip straight to multipart — peak disk is ONE part,
                # so any size (50 GB … 500 GB) works regardless of free space.
                writer = _StreamingMultipart(target['part_urls'],
                                             target['part_size'], base)
                try:
                    with zipfile.ZipFile(writer, 'w', zipfile.ZIP_DEFLATED,
                                         allowZip64=True) as z:
                        _write_zip_contents(z, conn, db, fs)
                    writer.close()
                except BaseException:
                    writer.abort()
                    raise
                results[db] = {'ok': True, 'mode': 'multipart', 'bytes': writer.total,
                               'upload_id': target.get('upload_id'),
                               'parts': writer.parts}
            else:
                # Small DB: build to disk and single-PUT.
                with tempfile.TemporaryDirectory(dir=base) as td:
                    zp = os.path.join(td, db + '.zip')
                    build_zip(conn, db, fs, zp)
                    res = _upload_zip(zp, target)
                    res['upload_id'] = res.get('upload_id') or target.get('upload_id')
                    results[db] = res
        except Exception as exc:  # noqa: BLE001
            results[db] = {'ok': False, 'error': str(exc),
                           'mode': target.get('mode'),
                           'upload_id': target.get('upload_id')}
    return results


def backup(mapfile):
    with open(mapfile) as fh:
        targets = json.load(fh)
    results = run_targets(targets)
    print(RESULT_MARKER + base64.b64encode(json.dumps(results).encode()).decode())


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else 'detect'
    if mode == 'detect':
        force = []
        if len(sys.argv) > 2 and sys.argv[2]:
            try:
                force = json.loads(base64.b64decode(sys.argv[2]).decode())
            except Exception:
                force = []
        detect(force)
    elif mode == 'backup' and len(sys.argv) > 2:
        backup(sys.argv[2])
    else:
        sys.stderr.write('usage: smart_backup.py detect [force_b64] | backup <mapfile>\n')
        sys.exit(2)


if __name__ == '__main__':
    main()

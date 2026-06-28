#!/usr/bin/env python3
"""Auto-detect every Odoo service on the host (name- and version-agnostic).

Run via the playbook's `script:` task so its Python source is never parsed by
Ansible's shell/quote splitter. Emits one base64-wrapped JSON line consumed by
server.host._parse_discovery. Read-only; self-escalates with passwordless sudo
only to read addons dirs / git repos owned by the odoo user.
"""
import json, os, re, subprocess, base64, shlex

try:
    import configparser
except ImportError:
    configparser = None


def sh(cmd):
    try:
        return subprocess.run(cmd, shell=True, capture_output=True,
                              text=True, timeout=60).stdout.strip()
    except Exception:
        return ""


# Addons dirs are usually owned by the odoo user (mode 700), so the SSH user
# cannot read them directly. Escalate with passwordless sudo when available
# (prefer the owning odoo user, fall back to root, then plain). Servers without
# sudo simply get no repo info.
SUDO = sh("sudo -n true >/dev/null 2>&1 && echo 1") == "1"


def run_as(user, cmd):
    attempts = []
    if SUDO and user:
        attempts.append("sudo -n -u %s bash -lc %s" % (shlex.quote(user), shlex.quote(cmd)))
    if SUDO:
        attempts.append("sudo -n bash -lc %s" % shlex.quote(cmd))
    attempts.append(cmd)
    for a in attempts:
        out = sh(a)
        if out:
            return out
    return ""


def git(root, args, user):
    # safe.directory=* bypasses the git dubious-ownership guard.
    return run_as(user, "git -c safe.directory='*' -C %s %s" % (shlex.quote(root), args))


# Per-run cache: repo URL -> [branch names]. Avoids repeating the (slow) ls-remote
# for a repository shared by several instances on the same host.
_BRANCH_CACHE = {}


def repo_branches(root, user):
    """All branch names for a repo, so the Pull wizard can offer every branch — not
    just the checked-out one.

    FAST PATH: the locally-known remote-tracking branches (`git branch -r`) — no
    network, and full clones already track every origin/* branch. Only if that is
    empty (e.g. a shallow / single-branch clone) do we hit the network with a
    bounded `ls-remote`. This keeps discovery fast (network calls per repo were the
    slow part)."""
    names = []
    for ln in (git(root, "branch -r", user) or '').splitlines():
        ln = ln.strip()
        if not ln or '->' in ln:
            continue
        names.append(re.sub(r'^[^/]+/', '', ln))
    if not names:  # shallow/single-branch clone — ask the remote (bounded)
        out = run_as(user, "timeout 20 git -c safe.directory='*' -C %s ls-remote "
                           "--heads origin" % shlex.quote(root))
        for ln in (out or '').splitlines():
            m = re.search(r'refs/heads/(.+)$', ln.strip())
            if m:
                names.append(m.group(1).strip())
    seen, res = set(), []
    for n in names:
        if n and n != 'HEAD' and n not in seen:
            seen.add(n)
            res.append(n)
    return res


def is_official_odoo(url):
    """True for the official Odoo org repos (odoo/odoo, odoo/enterprise, ...) —
    the framework source, which is never a pull target here."""
    return bool(re.search(r'github\.com[:/]odoo/', url or ''))


def clean_url(url):
    """Strip any embedded credentials (user:token@) from a git remote URL.

    Deployments often bake a GitHub token into the remote; it must never be
    stored or logged, and the pull playbook adds its own credentials, so the
    canonical URL is the one without them."""
    return re.sub(r'://[^/@]+@', '://', (url or '').strip())


def find_repos(addons_path, user):
    """Detect git repos under the addons path: their URL, branch and path.

    Smart about layout: an addons dir that is itself inside a git repo is
    recorded as that repo (so the Odoo source checkout and custom-addons repos
    are both found); a dir that merely contains repos is scanned one level deep
    so each module repo is found too.
    """
    found = {}
    dirs = [d.strip() for d in (addons_path or '').split(',') if d.strip()]
    for d in dirs:
        top = git(d, 'rev-parse --show-toplevel', user)
        candidates = []
        if top:
            candidates.append(top)
        else:
            for name in run_as(user, "ls -1 %s" % shlex.quote(d)).splitlines():
                name = name.strip()
                if not name:
                    continue
                ctop = git(os.path.join(d, name), 'rev-parse --show-toplevel', user)
                if ctop:
                    candidates.append(ctop)
        for root in candidates:
            if root in found:
                continue
            url = clean_url(git(root, 'config --get remote.origin.url', user))
            if not url:
                continue  # skip local-only repos with no remote
            if is_official_odoo(url):
                continue  # never a pull target — and skip its huge ls-remote
            branch = git(root, 'rev-parse --abbrev-ref HEAD', user)
            # Cache branches per repo URL so a repo shared by several instances is
            # listed once (ls-remote per repo is the slow part of discovery).
            if url not in _BRANCH_CACHE:
                _BRANCH_CACHE[url] = repo_branches(root, user)
            found[root] = {'path': root, 'url': url, 'branch': branch or '',
                           'branches': _BRANCH_CACHE[url]}
    return list(found.values())


def _has_odoo_bin(path):
    """True if `path` is the root of an Odoo source tree (holds odoo-bin/odoo.py)."""
    return any(os.path.isfile(os.path.join(path, b))
               for b in ('odoo-bin', 'odoo.py'))


def is_core_addons_dir(d):
    """True if `d` is one of Odoo's OWN addons dirs (the framework + standard apps
    that ship with Odoo), so its modules are core — never offered for upgrade.

    Odoo core addons always sit at ``<src>/addons`` or ``<src>/odoo/addons``, right
    next to the source tree's odoo-bin. Detecting that by structure (rather than by
    a git remote of github.com/odoo) works even when the Odoo source is a checkout
    from a fork/mirror or an unpacked tarball with no remote at all, and it catches
    the common layout where several Odoo source copies appear in one addons_path."""
    d = d.rstrip('/')
    if os.path.basename(d) != 'addons':
        return False
    parent = os.path.dirname(d)                       # <src>/addons
    if _has_odoo_bin(parent):
        return True
    if os.path.basename(parent) == 'odoo':            # <src>/odoo/addons
        return _has_odoo_bin(os.path.dirname(parent))
    return False


def find_custom_modules(addons_path, user, core_roots):
    """List the *custom* Odoo modules under the addons path (a module = a dir
    with __manifest__.py / __openerp__.py), excluding anything inside the Odoo
    core source so only the user's own modules are offered for upgrade."""
    mods = set()
    for d in [x.strip() for x in (addons_path or '').split(',') if x.strip()]:
        if any(d == cr or d.startswith(cr.rstrip('/') + '/') for cr in core_roots):
            continue  # skip core dirs detected via git remote (odoo/odoo, odoo/enterprise)
        if is_core_addons_dir(d):
            continue  # skip Odoo's own addons dirs detected by source-tree structure
        out = run_as(user, "find %s -mindepth 2 -maxdepth 2 "
                           "\\( -name __manifest__.py -o -name __openerp__.py \\)"
                     % shlex.quote(d))
        for line in out.splitlines():
            line = line.strip()
            if line:
                mods.add(os.path.basename(os.path.dirname(line)))
    return sorted(mods)


def find_core_modules(addons_path, user, core_roots):
    """List Odoo's bundled (core) modules under the addons path — the inverse of
    find_custom_modules — so they can be offered for upgrade alongside the custom
    ones (the user may need to upgrade e.g. `account` or `web` too)."""
    mods = set()
    for d in [x.strip() for x in (addons_path or '').split(',') if x.strip()]:
        is_core = any(d == cr or d.startswith(cr.rstrip('/') + '/') for cr in core_roots)
        if not is_core and not is_core_addons_dir(d):
            continue
        out = run_as(user, "find %s -mindepth 2 -maxdepth 2 "
                           "\\( -name __manifest__.py -o -name __openerp__.py \\)"
                     % shlex.quote(d))
        for line in out.splitlines():
            line = line.strip()
            if line:
                mods.add(os.path.basename(os.path.dirname(line)))
    return sorted(mods)


units = set()
for line in sh("systemctl list-unit-files --type=service --no-legend --plain").splitlines():
    parts = line.split()
    if parts:
        units.add(parts[0])
for line in sh("systemctl list-units --type=service --all --no-legend --plain").splitlines():
    parts = line.split()
    if parts:
        units.add(parts[0])


def conf_get(conf, key):
    if not conf or not os.path.exists(conf):
        return None
    try:
        if configparser:
            cp = configparser.ConfigParser(strict=False)
            cp.read(conf)
            if cp.has_section('options') and cp.has_option('options', key):
                return cp.get('options', key).strip()
        for ln in open(conf, 'r', errors='ignore'):
            ln = ln.strip()
            if ln.startswith(key) and '=' in ln:
                return ln.split('=', 1)[1].strip()
    except Exception:
        return None
    return None


def detect_version(odoo_bin, python_bin):
    if odoo_bin:
        m = re.search(r'odoo[-_]?(\d+\.\d+)', odoo_bin)
        if m:
            return m.group(1)
        rel = os.path.join(os.path.dirname(odoo_bin), 'odoo', 'release.py')
        try:
            txt = open(rel, errors='ignore').read()
            m = re.search(r"version_info\s*=\s*\(([^)]+)\)", txt)
            if m:
                nums = re.findall(r'\d+', m.group(1))
                if len(nums) >= 2:
                    return "%s.%s" % (nums[0], nums[1])
        except Exception:
            pass
        out = sh("%s %s --version 2>/dev/null" % (python_bin or 'python3', odoo_bin))
        m = re.search(r'(\d+\.\d+)', out)
        if m:
            return m.group(1)
    return ""


def _extract_blocks(text, keyword):
    blocks = []
    i = 0
    while True:
        idx = text.find(keyword, i)
        if idx == -1:
            break
        brace = text.find('{', idx)
        if brace == -1:
            break
        depth = 0
        j = brace
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
    """Map a proxied Odoo port -> {'domain', 'listen', 'file'} from nginx configs.

    The odoo instance is matched to its nginx vhost BY PORT (proxy_pass port ==
    the instance's http_port). 'file' is the site-config path so discovery can show
    which nginx file drives each instance."""
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
        for block in _extract_blocks(text, 'server'):
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
            ports = re.findall(r'proxy_pass\s+https?://(?:\d{1,3}(?:\.\d{1,3}){3}|localhost|127\.0\.0\.1):(\d+)', block)
            for up in re.findall(r'proxy_pass\s+https?://([A-Za-z_][\w.-]*)', block):
                ports.extend(upstreams.get(up, []))
            for prt in ports:
                cur = port_info.get(prt)
                if cur is None or (not cur.get('domain') and primary):
                    port_info[prt] = {'domain': primary, 'listen': listen, 'file': path}
    return port_info


port_info = parse_nginx()


def web_base_url_domain(conf):
    """Fallback domain for naming: the instance's own web.base.url host. Used
    when nginx doesn't reveal a domain. Reads the DB connection from the conf
    (local peer auth, or remote TCP) and queries ir_config_parameter. Needs a
    single db_name in the conf; returns '' if it's a bare IP / not set."""
    if not conf:
        return ''
    db_name = (conf_get(conf, 'db_name') or '').strip()
    if not db_name or db_name.lower() in ('false', 'none'):
        return ''
    db_host = (conf_get(conf, 'db_host') or '').strip()
    db_port = (conf_get(conf, 'db_port') or '').strip()
    db_user = (conf_get(conf, 'db_user') or '').strip()
    db_pass = conf_get(conf, 'db_password') or ''
    sql = "SELECT value FROM ir_config_parameter WHERE key='web.base.url'"
    env = dict(os.environ)
    env['LC_ALL'] = 'C'
    env['PGCONNECT_TIMEOUT'] = '5'
    local = db_host in ('', 'localhost', '127.0.0.1', '::1')
    if local:
        cmd = (['sudo', '-n', '-u', 'postgres'] if SUDO else []) + \
              ['psql', '-w', '-tAc', sql, '-d', db_name]
    else:
        cmd = ['psql', '-w', '-tAc', sql, '-d', db_name, '-h', db_host, '-U', db_user]
        if db_port:
            cmd += ['-p', db_port]
        if db_pass:
            env['PGPASSWORD'] = db_pass
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, env=env, timeout=12)
        val = r.stdout.strip() if r.returncode == 0 else ''
    except Exception:
        val = ''
    m = re.match(r'^\s*https?://([^/:]+)', val) if val else None
    host = (m.group(1) if m else '').strip().lower()
    if host in ('', 'localhost', '127.0.0.1', '0.0.0.0', '::1'):
        return ''
    if re.match(r'^\d{1,3}(\.\d{1,3}){3}$', host):   # bare IP, not a domain
        return ''
    return host


def scan_unit(unit):
    """Return the discovery dict for one systemd unit, or None if it is not an
    Odoo service. Raises only on unexpected errors (caller skips that unit)."""
    cat = sh("systemctl cat %s 2>/dev/null" % unit)
    m = re.search(r'^\s*ExecStart=(.*)$', cat, re.M)
    if not m:
        return None
    execline = m.group(1).strip()
    if 'odoo-bin' not in execline:
        return None
    toks = execline.split()
    odoo_bin = next((t for t in toks if t.endswith('odoo-bin')), '')
    python_bin = next((t for t in toks if re.search(r'/python[0-9.]*$', t)), '')
    conf = None
    for i, t in enumerate(toks):
        if t in ('-c', '--config') and i + 1 < len(toks):
            conf = toks[i + 1]
            break
        if t.startswith('--config='):
            conf = t.split('=', 1)[1]
            break
        if t.startswith('-c='):
            conf = t.split('=', 1)[1]
            break
    um = re.search(r'^\s*User=(.*)$', cat, re.M)
    odoo_user = um.group(1).strip() if um else ''
    http_port = conf_get(conf, 'http_port') or conf_get(conf, 'xmlrpc_port') or ''

    # Master password — only useful in plaintext; Odoo may store a pbkdf2 hash
    # (starts with "$") which cannot be replayed as the backup master_pwd.
    admin_pw = conf_get(conf, 'admin_passwd') or ''
    if admin_pw.startswith('$'):
        admin_pw = ''

    addons_path = conf_get(conf, 'addons_path') or ''
    repos = find_repos(addons_path, odoo_user)
    # Official Odoo repos (odoo/odoo, odoo/enterprise, ...) are the framework
    # source — nobody pulls them via this tool, so they are kept out of the pull
    # targets and used only to mark which addons dirs are "core".
    core_roots = [r['path'] for r in repos if is_official_odoo(r['url'])]
    modules = find_custom_modules(addons_path, odoo_user, core_roots)
    odoo_modules = find_core_modules(addons_path, odoo_user, core_roots)
    repos = [r for r in repos if not is_official_odoo(r['url'])]

    # Log file path: try the conf first, then the systemd unit.
    log_file = conf_get(conf, 'logfile') or ''
    if not log_file:
        for i, t in enumerate(toks):
            if t in ('--logfile', '--log-file') and i + 1 < len(toks):
                log_file = toks[i + 1]
                break
            if t.startswith('--logfile='):
                log_file = t.split('=', 1)[1]
                break
            if t.startswith('--log-file='):
                log_file = t.split('=', 1)[1]
                break
    if not log_file:
        m = re.search(r'>>?\s*(\S+\.log)', execline)
        if m:
            log_file = m.group(1)
    if not log_file:
        m = re.search(r'^\s*Standard(?:Output|Error)=(?:append|file):(\S+)', cat, re.M)
        if m:
            log_file = m.group(1)

    # Domain + nginx file + public port for this instance — matched to its nginx
    # vhost BY PORT (proxy_pass port == http_port). The stage name and the backup
    # path both use this exact source: the nginx domain, else <ip>:<port> where the
    # port is the nginx listen port (domainless vhost) or the conf http_port. (No
    # web.base.url fallback — that is not how the agent/backup resolves it.)
    nginx = (port_info.get(str(http_port)) if http_port else None) or {}
    domain = nginx.get('domain') or ''
    nginx_file = nginx.get('file') or ''
    pub_port = '' if domain else (nginx.get('listen') or str(http_port or ''))

    return {
        'service_name': unit[:-len('.service')] if unit.endswith('.service') else unit,
        'odoo_version': detect_version(odoo_bin, python_bin),
        'odoo_bin': odoo_bin,
        'python_bin': python_bin,
        'conf_file': conf or '',
        'odoo_user': odoo_user,
        'log_file': log_file,
        'http_port': http_port,
        'domain': domain,
        'pub_port': pub_port,
        'nginx_file': nginx_file,
        'addons_path': addons_path,
        'data_dir': conf_get(conf, 'data_dir') or '',
        'admin_passwd': admin_pw,
        'repos': repos,
        'modules': modules,
        'odoo_modules': odoo_modules,
    }


results = []
for unit in sorted(u for u in units if u.endswith('.service')):
    # One malformed unit must never abort the whole scan.
    try:
        inst = scan_unit(unit)
    except Exception:
        continue
    if inst:
        results.append(inst)

print("ODOO_DISCOVERY_JSON:" + base64.b64encode(json.dumps(results).encode()).decode())

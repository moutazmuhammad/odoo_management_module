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
            branch = git(root, 'rev-parse --abbrev-ref HEAD', user)
            found[root] = {'path': root, 'url': url, 'branch': branch or ''}
    return list(found.values())


def find_custom_modules(addons_path, user, core_roots):
    """List the *custom* Odoo modules under the addons path (a module = a dir
    with __manifest__.py / __openerp__.py), excluding anything inside the Odoo
    core source so only the user's own modules are offered for upgrade."""
    mods = set()
    for d in [x.strip() for x in (addons_path or '').split(',') if x.strip()]:
        if any(d == cr or d.startswith(cr.rstrip('/') + '/') for cr in core_roots):
            continue  # skip the core odoo/odoo addons dirs
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
    """Map proxied Odoo port -> primary domain from nginx config."""
    port_domain = {}
    dirs = ['/etc/nginx/sites-enabled', '/etc/nginx/sites-enable', '/etc/nginx/conf.d']
    text_all = ''
    for d in dirs:
        if not os.path.isdir(d):
            continue
        try:
            names = os.listdir(d)
        except Exception:
            continue
        for fn in sorted(names):
            p = os.path.join(d, fn)
            try:
                if os.path.isfile(p):
                    text_all += '\n' + open(p, errors='ignore').read()
            except Exception:
                pass
    if not text_all.strip():
        return port_domain
    upstreams = {}
    for m in re.finditer(r'upstream\s+(\S+)\s*\{([^}]*)\}', text_all, re.S):
        upstreams[m.group(1)] = re.findall(r'server\s+[^;]*?:(\d+)', m.group(2))
    for block in _extract_blocks(text_all, 'server'):
        domains = []
        for sn in re.findall(r'server_name\s+([^;]+);', block):
            for tok in sn.split():
                tok = tok.strip()
                if tok and tok not in ('_', 'localhost') and not tok.startswith('$'):
                    domains.append(tok)
        if not domains:
            continue
        primary = domains[0]
        ports = re.findall(r'proxy_pass\s+https?://(?:\d{1,3}(?:\.\d{1,3}){3}|localhost|127\.0\.0\.1):(\d+)', block)
        for up in re.findall(r'proxy_pass\s+https?://([A-Za-z_][\w.-]*)', block):
            ports.extend(upstreams.get(up, []))
        for prt in ports:
            port_domain.setdefault(prt, primary)
    return port_domain


port_domain = parse_nginx()


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

    return {
        'service_name': unit[:-len('.service')] if unit.endswith('.service') else unit,
        'odoo_version': detect_version(odoo_bin, python_bin),
        'odoo_bin': odoo_bin,
        'python_bin': python_bin,
        'conf_file': conf or '',
        'odoo_user': odoo_user,
        'log_file': log_file,
        'http_port': http_port,
        'domain': port_domain.get(str(http_port), '') if http_port else '',
        'addons_path': addons_path,
        'data_dir': conf_get(conf, 'data_dir') or '',
        'admin_passwd': admin_pw,
        'repos': repos,
        'modules': modules,
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

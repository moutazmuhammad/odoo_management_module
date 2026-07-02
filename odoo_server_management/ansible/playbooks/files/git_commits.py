#!/usr/bin/env python3
"""Report the current HEAD commit of each given git checkout, in one SSH session.

argv[1] = base64(json) of {"repos": [{"path": "...", "user": "..."}, ...]}
output  = ODOO_GITCOMMITS_JSON:<base64 json of
          {path: {"commit", "commit_short", "subject", "date", "author"}}>

Read-only. Self-escalates with passwordless sudo (to the checkout's owner, then
root) only to read repos owned by the odoo user — same approach as discover.py.
"""
import os
import sys
import json
import shlex
import base64
import subprocess

SUDO = os.path.exists('/usr/bin/sudo') or os.path.exists('/bin/sudo')

# git pretty-format: full sha, short sha, subject, committer ISO date, author name,
# each separated by the \x1f unit-separator byte so subjects parse unambiguously.
FMT = "%H%x1f%h%x1f%s%x1f%cI%x1f%an"


def sh(cmd):
    try:
        r = subprocess.run(cmd, shell=True, stdout=subprocess.PIPE,
                           stderr=subprocess.PIPE, universal_newlines=True, timeout=30)
        return r.stdout.strip() if r.returncode == 0 else ''
    except Exception:
        return ''


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
    return ''


def head_commit(path, user):
    # safe.directory=* bypasses git's dubious-ownership guard.
    cmd = ("git -c safe.directory='*' -C " + shlex.quote(path) +
           " log -1 --format=" + shlex.quote(FMT))
    return run_as(user, cmd)


try:
    spec = json.loads(base64.b64decode(sys.argv[1]).decode())
except Exception:
    spec = {}

out = {}
for r in (spec.get('repos') or []):
    path = (r.get('path') or '').strip()
    user = (r.get('user') or '').strip()
    if not path:
        continue
    line = head_commit(path, user)
    if not line:
        continue
    p = line.split('\x1f')
    out[path] = {
        'commit':       p[0].strip() if len(p) > 0 else '',
        'commit_short': p[1].strip() if len(p) > 1 else '',
        'subject':      p[2].strip() if len(p) > 2 else '',
        'date':         p[3].strip() if len(p) > 3 else '',
        'author':       p[4].strip() if len(p) > 4 else '',
    }

print("ODOO_GITCOMMITS_JSON:" + base64.b64encode(json.dumps(out).encode()).decode())

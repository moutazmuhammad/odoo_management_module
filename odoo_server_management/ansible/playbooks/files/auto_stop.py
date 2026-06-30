#!/usr/bin/env python3
"""Stop systemd services that have been active longer than N days.

argv[1] = base64(json) of {"days": N, "services": ["svc1", ...]}
output  = ODOO_AUTOSTOP_JSON:<base64 json list of stopped service names>

Only currently-active services older than the threshold are stopped and
disabled, via passwordless sudo (same privilege the manual Stop action uses).
"""
import sys
import json
import base64
import shlex
import subprocess


def sh(cmd):
    try:
        return subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=30)
    except Exception:
        return None


try:
    spec = json.loads(base64.b64decode(sys.argv[1]).decode())
except Exception:
    spec = {}

days = int(spec.get('days') or 0)
services = spec.get('services') or []
threshold = days * 86400

now = 0
r = sh('date +%s')
if r and r.stdout.strip():
    now = int(r.stdout.strip())

stopped = []
if threshold and now:
    for svc in services:
        q = shlex.quote(svc)
        r = sh('systemctl show %s -p ActiveState -p ActiveEnterTimestamp --value 2>/dev/null' % q)
        if not r or r.returncode != 0:
            continue
        lines = [ln.strip() for ln in (r.stdout or '').splitlines()]
        if len(lines) < 2:
            continue
        active_state, ts = lines[0], lines[1]
        if active_state != 'active' or not ts:
            continue
        d = sh('date -d %s +%%s 2>/dev/null' % shlex.quote(ts))
        if not d or not d.stdout.strip():
            continue
        started = int(d.stdout.strip())
        if (now - started) >= threshold:
            st = sh('sudo -n systemctl stop %s' % q)
            if st and st.returncode == 0:
                # Also disable, so the instance stays off across a reboot — same as
                # the manual Stop action (stop_service.yml: enabled=no).
                sh('sudo -n systemctl disable %s' % q)
                stopped.append(svc)

print("ODOO_AUTOSTOP_JSON:" + base64.b64encode(json.dumps(stopped).encode()).decode())

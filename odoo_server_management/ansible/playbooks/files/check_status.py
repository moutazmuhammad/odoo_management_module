#!/usr/bin/env python3
"""Report the real systemd status of several services in one SSH session.
argv[1] = base64(json) list of service names.
output  = ODOO_STATUS_JSON:<base64 json {service: 'active'|'inactive'|...}>"""
import sys
import json
import base64
import subprocess

try:
    services = json.loads(base64.b64decode(sys.argv[1]).decode())
except Exception:
    services = []

out = {}
for s in services:
    if not s:
        continue
    try:
        # NOTE: capture_output= and text= are Python 3.7+. Target hosts may run
        # Python 3.6 (e.g. Ubuntu 18.04), where those kwargs raise TypeError, so
        # every probe would fall to 'unknown' and a RUNNING service would be shown
        # as stopped. Use the 3.6-compatible stdout/stderr=PIPE + universal_newlines.
        r = subprocess.run(['systemctl', 'is-active', s],
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                           universal_newlines=True, timeout=10)
        out[s] = (r.stdout or '').strip() or 'unknown'
    except Exception:
        out[s] = 'unknown'

print('ODOO_STATUS_JSON:' + base64.b64encode(json.dumps(out).encode()).decode())

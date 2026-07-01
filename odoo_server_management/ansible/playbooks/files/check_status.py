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
        r = subprocess.run(['systemctl', 'is-active', s],
                           capture_output=True, text=True, timeout=10)
        out[s] = (r.stdout or '').strip() or 'unknown'
    except Exception:
        out[s] = 'unknown'

print('ODOO_STATUS_JSON:' + base64.b64encode(json.dumps(out).encode()).decode())

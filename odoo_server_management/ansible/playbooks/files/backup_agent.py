#!/usr/bin/env python3
"""Standalone per-server backup agent (runs from local cron, as root).

Flow — nothing but a throwaway URL ever touches this server:
  1. Detect the EXPOSED databases on this server (smart_backup, ownership +
     canonical selection + the host override list baked into the config).
  2. Ask the manager for short-lived pre-signed upload URLs (auth = per-host
     token). The Spaces key stays on the manager.
  3. Dump + zip + upload each DB directly to the Space via those URLs.
  4. Report results so the manager can complete multipart uploads, prune and
     record last_backup.

Config: /etc/odoo-backup.conf (key=value), root-only 0600:
  manager_url = http://exp.odex.sa
  token       = <per-host agent token>
  extra_dbs   = db_one, db_two        # optional override (multi-DB/ambiguous)
  insecure    = 0                      # 1 = skip TLS verify (self-signed manager)
"""
import os
import ssl
import sys
import json
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import smart_backup as sb  # noqa: E402

CONF = '/etc/odoo-backup.conf'


def load_conf(path=CONF):
    cfg = {}
    with open(path) as fh:
        for ln in fh:
            ln = ln.strip()
            if ln and not ln.startswith('#') and '=' in ln:
                k, v = ln.split('=', 1)
                cfg[k.strip()] = v.strip()
    return cfg


def post(url, payload, insecure=False, host_header='', timeout=180):
    data = json.dumps({'jsonrpc': '2.0', 'method': 'call',
                       'params': payload}).encode()
    headers = {'Content-Type': 'application/json'}
    if host_header:
        # Reach the manager by IP but route to the right nginx vhost (avoids any
        # DNS dependency on the managed servers).
        headers['Host'] = host_header
    req = urllib.request.Request(url, data=data, headers=headers)
    ctx = None
    if url.lower().startswith('https'):
        ctx = ssl.create_default_context()
        if insecure:
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
    resp = json.loads(urllib.request.urlopen(req, timeout=timeout, context=ctx).read().decode())
    if resp.get('error'):
        raise RuntimeError('manager error: %s' % resp['error'])
    result = resp.get('result') or {}
    if isinstance(result, dict) and result.get('error'):
        raise RuntimeError('manager: %s' % result['error'])
    return result


def main():
    cfg = load_conf()
    base = cfg['manager_url'].rstrip('/')
    token = cfg['token']
    host_header = cfg.get('host_header', '').strip()
    insecure = cfg.get('insecure', '0') in ('1', 'true', 'True')
    force = [x.strip() for x in cfg.get('extra_dbs', '').replace('\n', ',').split(',')
             if x.strip()]

    items, skipped = sb.detect_items(force)
    # Optional targeted run (testing / manual): ODOO_BACKUP_ONLY=db1,db2
    only = [x.strip() for x in os.environ.get('ODOO_BACKUP_ONLY', '').split(',')
            if x.strip()]
    if only:
        items = [it for it in items if it['db'] in only]
    if not items:
        print('backup-agent: no exposed databases detected')
        return

    # Process ONE database at a time — pre-sign, dump+upload, finalize — then move
    # to the next. This bounds in-flight multipart uploads to one, avoids
    # pre-signed URLs expiring during a long run, and records progress per DB.
    ok = 0
    for it in items:
        db = it['db']
        try:
            req = [{'db': db, 'domain': it.get('domain') or '',
                    'port': it.get('port') or '',
                    'size': int(it.get('size') or 0)}]
            targets = post(base + '/server_backup/agent/presign',
                           {'token': token, 'dbs': req}, insecure, host_header).get('targets') or {}
            target = targets.get(db)
            if not target:
                print('backup-agent: %s skipped (no target)' % db)
                continue
            target['filestore'] = it.get('filestore', '')
            res = sb.run_targets({db: target}).get(db) or {'ok': False, 'error': 'no result'}
            res['key'] = target.get('key')
            fin = post(base + '/server_backup/agent/finalize',
                       {'token': token, 'results': {db: res}}, insecure, host_header)
            if res.get('ok') and fin.get('ok'):
                ok += 1
                print('backup-agent: %s uploaded' % db)
            else:
                print('backup-agent: %s FAILED (%s)' % (db, res.get('error') or 'finalize'))
        except Exception as exc:  # noqa: BLE001 — one DB failing must not stop the rest
            print('backup-agent: %s ERROR %s' % (db, str(exc)[:200]))

    # Final pass: prune old objects + stamp last_backup once.
    try:
        post(base + '/server_backup/agent/finalize',
             {'token': token, 'results': {}, 'done': True}, insecure, host_header)
    except Exception as exc:  # noqa: BLE001
        print('backup-agent: prune step failed: %s' % str(exc)[:200])
    print('backup-agent: %s/%s uploaded' % (ok, len(items)))


if __name__ == '__main__':
    main()

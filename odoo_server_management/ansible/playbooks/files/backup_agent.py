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


class _KeepPostRedirect(urllib.request.HTTPRedirectHandler):
    """Follow 301/302/303/307/308 while KEEPING the POST method + JSON body.

    Python's default handler downgrades POST→GET on 301/302/303. The manager is
    typically reached over http-by-IP (Host: <vhost>) and nginx answers with a
    301 to https://<vhost>/…; the default behaviour would turn our POST into a
    GET, which the POST-only JSON routes reject with 405/400 — so the agent could
    never even fetch its DB list. We re-issue the SAME POST (body + headers) to
    the redirect target instead."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if code not in (301, 302, 303, 307, 308):
            return None
        newheaders = {k: v for k, v in req.header_items()
                      if k.lower() not in ('content-length',)}
        return urllib.request.Request(
            newurl, data=req.data, headers=newheaders,
            origin_req_host=req.origin_req_host, unverifiable=True,
            method='POST')


def post(url, payload, insecure=False, host_header='', timeout=180):
    data = json.dumps({'jsonrpc': '2.0', 'method': 'call',
                       'params': payload}).encode()
    headers = {'Content-Type': 'application/json'}
    if host_header:
        # Reach the manager by IP but route to the right nginx vhost (avoids any
        # DNS dependency on the managed servers). Carried across the redirect too.
        headers['Host'] = host_header
    # An ssl context is always prepared: even when `url` is http, the manager may
    # 301 us to https, and the opener's HTTPS handler needs the (optionally
    # insecure) context ready for that hop.
    ctx = ssl.create_default_context()
    if insecure:
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    opener = urllib.request.build_opener(
        _KeepPostRedirect(), urllib.request.HTTPSHandler(context=ctx))
    req = urllib.request.Request(url, data=data, headers=headers, method='POST')
    resp = json.loads(opener.open(req, timeout=timeout).read().decode())
    if resp.get('error'):
        raise RuntimeError('manager error: %s' % resp['error'])
    result = resp.get('result') or {}
    if isinstance(result, dict) and result.get('error'):
        raise RuntimeError('manager: %s' % result['error'])
    return result


def main():
    cfg = load_conf()
    base = cfg['manager_url'].rstrip('/')
    # No secret is stored on this server: the manager identifies us by source IP.
    # A token is only sent if a legacy config still has one (migration fallback).
    token = cfg.get('token', '').strip()
    host_header = cfg.get('host_header', '').strip()
    insecure = cfg.get('insecure', '0') in ('1', 'true', 'True')
    force = [x.strip() for x in cfg.get('extra_dbs', '').replace('\n', ',').split(',')
             if x.strip()]

    # Authoritative target list from the manager — EVERY database of EVERY stage,
    # with its path segment — fetched each run so the agent always has the newest
    # set (the manager keeps it fresh via discovery + the DB-refresh cron). The
    # local extra_dbs is an additive override.
    targets = post(base + '/server_backup/agent/dblist',
                   {'token': token}, insecure, host_header).get('targets') or []
    have = {t.get('db') for t in targets}
    for db in force:
        if db and db not in have:
            targets.append({'db': db, 'domain': '', 'port': ''})
            have.add(db)
    items = sb._size_targets(targets)
    # Optional targeted run (testing / manual): ODOO_BACKUP_ONLY=db1,db2
    only = [x.strip() for x in os.environ.get('ODOO_BACKUP_ONLY', '').split(',')
            if x.strip()]
    if only:
        items = [it for it in items if it['db'] in only]
    if not items:
        print('backup-agent: no databases to back up')
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

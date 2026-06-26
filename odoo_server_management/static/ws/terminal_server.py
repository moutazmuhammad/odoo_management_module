#!/usr/bin/env python3
"""Standalone WebSocket <-> SSH-PTY bridge for the admin web terminal.

A real interactive terminal needs bidirectional streaming + a PTY, which Odoo's
HTTP worker model cannot provide; this small process does it. The browser
(xterm.js) connects to ws://.../terminal/ws/<host_id>?token=..., this server
verifies the short-lived HMAC token minted by the (admin-only) Odoo controller,
opens an interactive SSH shell to the host with the global key, and pipes bytes
both ways. Resize events are forwarded to the PTY.

Run it next to Odoo (systemd) and, in production, have nginx proxy
`/terminal/ws/` to it (127.0.0.1:8770). Env:
  TERM_WS_PORT (8770), TERM_WS_BIND (127.0.0.1), ODOO_DB,
  ODOO_DB_HOST/USER/PASSWORD, ODOO_ADDONS_PATH.
"""
import os
import json
import time
import hmac
import hashlib
import asyncio

import websockets
import paramiko

from urllib.parse import urlparse, parse_qs

WS_PORT = int(os.environ.get('TERM_WS_PORT', 8770))
WS_BIND = os.environ.get('TERM_WS_BIND', '127.0.0.1')
ODOO_DB = os.environ.get('ODOO_DB', 'odoo')
GROUP_ADMIN = 'odoo_server_management.group_admin'

# --- Load Odoo (no odoo-bin needed; works for apt/pip installs too) ----------
import odoo  # noqa: E402
from odoo.tools import config  # noqa: E402

config['db_name'] = ODOO_DB
config['db_host'] = os.environ.get('ODOO_DB_HOST') or os.environ.get('HOST') or 'localhost'
config['db_port'] = int(os.environ.get('ODOO_DB_PORT') or 5432)
config['db_user'] = os.environ.get('ODOO_DB_USER') or os.environ.get('USER') or 'odoo'
config['db_password'] = os.environ.get('ODOO_DB_PASSWORD') or os.environ.get('PASSWORD') or 'odoo'
if os.environ.get('ODOO_ADDONS_PATH'):
    config['addons_path'] = os.environ['ODOO_ADDONS_PATH']

from odoo import api, SUPERUSER_ID  # noqa: E402
from odoo.modules.registry import Registry  # noqa: E402


def _verify_and_load(host_id, token):
    """Validate the token (admin-minted) and return connection info, or None."""
    try:
        sid, uid, exp, sig = token.split('.')
    except (ValueError, AttributeError):
        return None
    if int(sid) != int(host_id) or int(exp) < int(time.time()):
        return None
    reg = Registry(ODOO_DB)
    with reg.cursor() as cr:
        env = api.Environment(cr, SUPERUSER_ID, {})
        secret = env['ir.config_parameter'].sudo().get_param('database.secret')
        good = hmac.new(secret.encode(), ('%s.%s.%s' % (sid, uid, exp)).encode(),
                        hashlib.sha256).hexdigest()
        if not hmac.compare_digest(good, sig):
            return None
        user = env['res.users'].browse(int(uid))
        if not user.exists() or not user.has_group(GROUP_ADMIN):
            return None
        host = env['server.host'].browse(int(host_id)).exists()
        if not host:
            return None
        Stage = env['server.stage']
        return {
            'ip': host.ip,
            'port': host.ssh_port or 22,
            'user': Stage._default_ssh_user(),
            'key_file': Stage._ssh_key_file(),
            'known_hosts': Stage._known_hosts_file(),
        }


async def handler(websocket, path=None):
    # Path location moved across websockets versions: v15 -> websocket.request.path,
    # v11-14 -> websocket.path, v10 -> passed as the 2nd arg.
    if path is None:
        req = getattr(websocket, 'request', None)
        path = getattr(req, 'path', None) or getattr(websocket, 'path', '') or ''
    parsed = urlparse(path)
    try:
        host_id = int(parsed.path.strip('/').split('/')[-1])
    except (ValueError, IndexError):
        await websocket.close()
        return
    token = parse_qs(parsed.query).get('token', [''])[0]

    loop = asyncio.get_event_loop()
    info = await loop.run_in_executor(None, _verify_and_load, host_id, token)
    if not info:
        await websocket.send('\r\n\x1b[31mUnauthorized or expired terminal token.\x1b[0m\r\n')
        await websocket.close()
        return

    ssh = paramiko.SSHClient()
    try:
        if info['known_hosts'] and os.path.exists(info['known_hosts']):
            ssh.load_host_keys(info['known_hosts'])
    except Exception:
        pass
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    # From here on, always close the ssh client + websocket — even if
    # connect()/invoke_shell() fails — so we never leak a Transport thread/socket.
    chan = None
    t1 = t2 = None
    try:
        try:
            await loop.run_in_executor(None, lambda: ssh.connect(
                hostname=info['ip'], port=info['port'], username=info['user'],
                key_filename=info['key_file'], look_for_keys=True, allow_agent=True,
                timeout=15))
        except Exception as exc:
            await websocket.send('\r\n\x1b[31mSSH connection failed: %s\x1b[0m\r\n' % exc)
            return
        try:
            ssh.save_host_keys(info['known_hosts'])
        except Exception:
            pass

        chan = ssh.invoke_shell(term='xterm-256color', width=120, height=30)
        chan.settimeout(0.0)

        async def ssh_to_ws():
            while True:
                await asyncio.sleep(0.01)
                try:
                    if chan.recv_ready():
                        data = chan.recv(8192)
                        if not data:
                            break
                        await websocket.send(data.decode('utf-8', errors='replace'))
                    elif chan.closed or chan.exit_status_ready():
                        break
                except Exception:
                    break

        async def ws_to_ssh():
            async for message in websocket:
                try:
                    m = json.loads(message)
                except (ValueError, TypeError):
                    chan.sendall(message)
                    continue
                if m.get('t') == 'i':
                    chan.sendall(m.get('d', ''))
                elif m.get('t') == 'r':
                    try:
                        chan.resize_pty(width=int(m['c']), height=int(m['r']))
                    except Exception:
                        pass

        t1 = asyncio.ensure_future(ssh_to_ws())
        t2 = asyncio.ensure_future(ws_to_ssh())
        await asyncio.wait([t1, t2], return_when=asyncio.FIRST_COMPLETED)
    finally:
        for t in (t1, t2):
            if t is not None:
                t.cancel()
        for closer in ((chan.close if chan else None), ssh.close, websocket.close):
            if closer is None:
                continue
            try:
                res = closer()
                if asyncio.iscoroutine(res):
                    await res
            except Exception:
                pass


async def main():
    async with websockets.serve(handler, WS_BIND, WS_PORT, ping_interval=20, max_size=None):
        print('Terminal WS server on %s:%s' % (WS_BIND, WS_PORT), flush=True)
        await asyncio.Future()


if __name__ == '__main__':
    asyncio.run(main())

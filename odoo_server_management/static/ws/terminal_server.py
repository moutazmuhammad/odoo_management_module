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
import logging
import hashlib
import asyncio

import websockets
import paramiko

from urllib.parse import urlparse, parse_qs

# Log to stderr so `journalctl -u odoo-terminal` shows the precise reason a
# session was rejected — token vs. SSH failures used to be indistinguishable.
logging.basicConfig(
    level=logging.INFO,
    format='[term-ws] %(asctime)s %(levelname)s %(message)s',
)
_log = logging.getLogger('terminal_ws')

WS_PORT = int(os.environ.get('TERM_WS_PORT', 8770))
WS_BIND = os.environ.get('TERM_WS_BIND', '127.0.0.1')
ODOO_DB = os.environ.get('ODOO_DB', 'odoo')
GROUP_ADMIN = 'odoo_server_management.group_admin'
# Optional overrides — let an operator pin the key/user the terminal uses
# without depending on Odoo's stored config (handy for first-run debugging).
TERM_SSH_KEY_FILE = os.environ.get('TERM_SSH_KEY_FILE')
TERM_SSH_USER = os.environ.get('TERM_SSH_USER')

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
    """Validate the admin-minted token and load connection info.

    Returns ``(info, None)`` on success or ``(None, reason)`` on failure, where
    ``reason`` is a short human string that is logged and shown in the terminal
    so the *actual* cause (expired token, DB/secret mismatch, missing key, …) is
    visible instead of a generic "Unauthorized"."""
    try:
        sid, uid, exp, sig = token.split('.')
    except (ValueError, AttributeError):
        return None, 'malformed token'
    try:
        sid_i, uid_i, exp_i = int(sid), int(uid), int(exp)
    except (TypeError, ValueError):
        return None, 'non-numeric token fields'
    if sid_i != int(host_id):
        return None, 'token host mismatch'
    now = int(time.time())
    if exp_i < now:
        return None, 'token expired %ss ago (clock skew between Odoo and bridge?)' % (now - exp_i)
    reg = Registry(ODOO_DB)
    with reg.cursor() as cr:
        env = api.Environment(cr, SUPERUSER_ID, {})
        secret = env['ir.config_parameter'].sudo().get_param('database.secret')
        if not secret:
            return None, 'no database.secret on db %r (wrong ODOO_DB?)' % ODOO_DB
        good = hmac.new(secret.encode(), ('%s.%s.%s' % (sid, uid, exp)).encode(),
                        hashlib.sha256).hexdigest()
        if not hmac.compare_digest(good, sig):
            return None, 'bad signature — bridge db %r differs from the web db' % ODOO_DB
        user = env['res.users'].browse(uid_i)
        if not user.exists():
            return None, 'user %s not found' % uid_i
        if not user.has_group(GROUP_ADMIN):
            return None, 'user %s is not a Server-Management Administrator' % uid_i
        host = env['server.host'].browse(int(host_id)).exists()
        if not host:
            return None, 'host %s not found' % host_id
        Stage = env['server.stage']
        key_file = TERM_SSH_KEY_FILE or Stage._ssh_key_file()
        if not key_file:
            return None, ('no SSH key configured — set the global key in '
                          'Server Management → SSH (or TERM_SSH_KEY_FILE)')
        if not os.path.exists(key_file):
            return None, 'SSH key file %r does not exist on the bridge host' % key_file
        return {
            'ip': host.ip,
            'port': host.ssh_port or 22,
            'user': TERM_SSH_USER or Stage._default_ssh_user(),
            'key_file': key_file,
            'known_hosts': Stage._known_hosts_file(),
        }, None


def _load_key(path):
    """Load a private key file, trying each algorithm. paramiko's implicit
    auto-detection inside connect() swallows the real reason a key won't load;
    doing it explicitly lets us report it. Returns (pkey, None) or (None, err)."""
    last = None
    for key_cls in (paramiko.Ed25519Key, paramiko.ECDSAKey, paramiko.RSAKey):
        try:
            return key_cls.from_private_key_file(path), None
        except paramiko.PasswordRequiredException as exc:
            return None, 'key is passphrase-protected (%s)' % exc
        except Exception as exc:  # noqa: BLE001 — wrong algo, try the next class
            last = exc
    return None, last


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
    info, reason = await loop.run_in_executor(None, _verify_and_load, host_id, token)
    if not info:
        _log.warning('terminal rejected for host %s: %s', host_id, reason)
        await websocket.send('\r\n\x1b[31mTerminal authorization failed: %s\x1b[0m\r\n' % reason)
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
        # Load the key ourselves so a bad/locked key gives a precise error
        # rather than a generic "Authentication failed". Fall back to letting
        # paramiko search (agent / key_filename) only if explicit load fails.
        pkey, key_err = _load_key(info['key_file'])
        if pkey is None:
            _log.error('could not load SSH key %s: %s', info['key_file'], key_err)
        connect_kwargs = dict(
            hostname=info['ip'], port=info['port'], username=info['user'],
            timeout=15, banner_timeout=15, auth_timeout=15)
        if pkey is not None:
            connect_kwargs.update(pkey=pkey, look_for_keys=False, allow_agent=False)
        else:
            connect_kwargs.update(key_filename=info['key_file'],
                                  look_for_keys=True, allow_agent=True)
        try:
            await loop.run_in_executor(None, lambda: ssh.connect(**connect_kwargs))
        except Exception as exc:
            _log.error('SSH connect to %s@%s:%s failed: %s: %s',
                       info['user'], info['ip'], info['port'],
                       type(exc).__name__, exc)
            await websocket.send(
                '\r\n\x1b[31mSSH connection failed (%s@%s:%s): %s\x1b[0m\r\n'
                % (info['user'], info['ip'], info['port'], exc))
            return
        _log.info('terminal opened: %s@%s:%s (host %s)',
                  info['user'], info['ip'], info['port'], host_id)
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

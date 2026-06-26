import time
import hmac
import hashlib

from odoo import http, _
from odoo.http import request
from odoo.exceptions import AccessError

GROUP_ADMIN = 'odoo_server_management.group_admin'
TOKEN_TTL = 60  # seconds — just long enough to open the socket


def _sign(secret, msg):
    return hmac.new(secret.encode(), msg.encode(), hashlib.sha256).hexdigest()


class ServerTerminalController(http.Controller):
    """Admin-only real terminal: renders an xterm.js page that connects to the
    WebSocket PTY bridge (static/ws/terminal_server.py). The session is the
    authorization; a short-lived signed token authenticates to the WS server."""

    def _admin_host(self, host_id):
        if not request.env.user.has_group(GROUP_ADMIN):
            raise AccessError(_("Only administrators can open a server terminal."))
        host = request.env['server.host'].sudo().browse(host_id).exists()
        if not host:
            raise request.not_found()
        return host

    @http.route('/server/terminal/<int:host_id>', auth='user', type='http', website=True)
    def terminal_page(self, host_id, **kwargs):
        host = self._admin_host(host_id)
        ICP = request.env['ir.config_parameter'].sudo()

        # Short-lived signed token verified by the WS server.
        secret = ICP.get_param('database.secret')
        exp = int(time.time()) + TOKEN_TTL
        payload = '%s.%s.%s' % (host_id, request.env.user.id, exp)
        token = '%s.%s' % (payload, _sign(secret, payload))

        # WS endpoint. Default assumes nginx proxies /terminal/ws to the bridge
        # on the same origin; override with the `server.terminal.ws_url` param
        # (e.g. ws://host:8770) when not behind a proxy.
        ws_base = ICP.get_param('server.terminal.ws_url')
        if not ws_base:
            base = ICP.get_param('web.base.url') or ''
            ws_base = base.replace('https://', 'wss://').replace('http://', 'ws://') + '/terminal/ws'
        ws_url = '%s/%s?token=%s' % (ws_base.rstrip('/'), host_id, token)

        Stage = request.env['server.stage']
        return request.render('odoo_server_management.server_terminal_template', {
            'host_id': host_id,
            'host_name': host.name or host.ip,
            'host_ip': host.ip,
            'ssh_user': Stage._default_ssh_user(),
            'ws_url': ws_url,
        })

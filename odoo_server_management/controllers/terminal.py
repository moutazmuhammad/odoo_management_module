import time
import hmac
import hashlib

from odoo import http, _
from odoo.http import request
from odoo.exceptions import AccessError

GROUP_ADMIN = 'odoo_server_management.group_admin'
TOKEN_TTL = 300  # seconds — headroom for page load + minor clock skew


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

        # WS endpoint. By default the page builds the URL from the browser's own
        # location (same origin) so it always matches how the admin reached Odoo
        # — host and http/https — instead of web.base.url, which may be a domain
        # the browser can't resolve (a real failure we hit: web.base.url pointed
        # at an unresolvable host, so the socket never opened). nginx proxies
        # /terminal/ws/ to the bridge on that same origin. Set the
        # `server.terminal.ws_url` param (e.g. ws://host:8770) only to force a
        # different origin/port, e.g. when the bridge is not behind the proxy.
        ws_path = '/terminal/ws/%s?token=%s' % (host_id, token)
        ws_override = ICP.get_param('server.terminal.ws_url')
        ws_url = ''
        if ws_override:
            ws_url = '%s/%s?token=%s' % (ws_override.rstrip('/'), host_id, token)

        Stage = request.env['server.stage']
        return request.render('odoo_server_management.server_terminal_template', {
            'host_id': host_id,
            'host_name': host.name or host.ip,
            'host_ip': host.ip,
            'ssh_user': Stage._default_ssh_user(),
            'ws_url': ws_url,
            'ws_path': ws_path,
        })

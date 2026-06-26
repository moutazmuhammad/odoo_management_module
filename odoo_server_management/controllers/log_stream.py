import time
import shlex
import select
import subprocess

from odoo import http, _
from odoo.http import request, Response
from odoo.exceptions import AccessError

# Hard cap so a forgotten browser tab can't hold a streaming request forever.
STREAM_MAX_SECONDS = 600
TAIL_LINES = 200

GROUP_USER = 'odoo_server_management.group_user'


class LiveLogStreamController(http.Controller):
    """Live log streaming via Server-Sent Events, served by Odoo itself.

    No standalone WebSocket process / reverse-proxy route is needed: the feed is
    a normal authenticated Odoo route (`auth='user'` + group check), same-origin,
    so the user session *is* the authorization. The server SSHes to the target
    with the global key and `tail -f`s the log, pushing each line as an SSE event.
    """

    def _authorized_stage(self, stage_id):
        stage = request.env['server.stage'].browse(stage_id).exists()
        if not stage:
            raise request.not_found()
        user = request.env.user
        if not user.has_group(GROUP_USER):
            raise AccessError(_("You are not allowed to view these logs."))
        return stage

    @http.route('/log/stream/<int:stage_id>', auth='user', type='http', website=True)
    def log_stream_page(self, stage_id, **kwargs):
        stage = self._authorized_stage(stage_id)
        return request.render(
            "odoo_server_management.log_stream_template",
            {'stage_id': stage_id, 'stage_name': stage.name or _('Instance %s') % stage_id},
        )

    @http.route('/log/stream/<int:stage_id>/feed', auth='user', type='http')
    def log_stream_feed(self, stage_id, **kwargs):
        stage = self._authorized_stage(stage_id).sudo()

        log_file = stage.log_file_path
        # Refuse anything with shell metacharacters (defense in depth; it is
        # quoted below too).
        if not log_file or any(c in log_file for c in ';|&$`\n><'):
            return Response(
                "data: ❌ No valid log file path is configured for this instance.\n\n",
                mimetype='text/event-stream',
            )

        # Capture everything needed BEFORE streaming — the generator runs after
        # the request's DB cursor is gone, so it must not touch the ORM.
        host = stage.host_id
        if not host:
            return Response(
                "data: ❌ This instance has no Server Host configured.\n\n",
                mimetype='text/event-stream',
            )
        ip = host.ip
        ssh_port = str(host.ssh_port or 22)
        Stage = request.env['server.stage']
        ssh_user = Stage._default_ssh_user()
        key_file = Stage._ssh_key_file()
        known_hosts = Stage._known_hosts_file()

        ssh_cmd = [
            'ssh', '-o', 'BatchMode=yes',
            '-o', 'StrictHostKeyChecking=accept-new',
            '-o', 'UserKnownHostsFile=%s' % known_hosts,
            '-o', 'ServerAliveInterval=15',
            '-p', ssh_port,
        ]
        if key_file:
            ssh_cmd += ['-i', key_file]
        # Read the log directly; if it isn't readable by the SSH user, fall back
        # to passwordless sudo (the same privilege the service actions rely on).
        quoted = shlex.quote(log_file)
        remote_cmd = (
            'tail -n %d -f %s 2>/dev/null || sudo -n tail -n %d -f %s'
            % (TAIL_LINES, quoted, TAIL_LINES, quoted)
        )
        ssh_cmd += ['%s@%s' % (ssh_user, ip), remote_cmd]

        def sse(text):
            # The WSGI server requires bytes, not str.
            return text.encode('utf-8')

        def generate():
            proc = None
            start = time.time()
            try:
                yield sse("retry: 10000\n\n")
                yield sse("data: \U0001f50c Connecting to %s …\n\n" % ip)
                proc = subprocess.Popen(
                    ssh_cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                    bufsize=1, text=True,
                )
                while True:
                    if time.time() - start > STREAM_MAX_SECONDS:
                        yield sse("data: ⏱️ Stream ended after 10 minutes — reload to resume.\n\n")
                        break
                    ready, _, _ = select.select([proc.stdout], [], [], 1.0)
                    if ready:
                        line = proc.stdout.readline()
                        if not line:
                            yield sse("data: ⚠️ Connection closed.\n\n")
                            break
                        yield sse("data: %s\n\n" % line.rstrip("\n"))
                    else:
                        # keepalive comment so proxies don't drop an idle stream
                        yield sse(": keepalive\n\n")
                        if proc.poll() is not None:
                            yield sse("data: ⚠️ Connection ended.\n\n")
                            break
            except GeneratorExit:
                raise
            except Exception as exc:  # noqa: BLE001
                yield sse("data: ❌ Error: %s\n\n" % exc)
            finally:
                if proc:
                    try:
                        if proc.poll() is None:
                            proc.terminate()
                        proc.wait(timeout=5)
                    except Exception:
                        try:
                            proc.kill()
                        except Exception:
                            pass
                    try:
                        if proc.stdout:
                            proc.stdout.close()
                    except Exception:
                        pass

        headers = [
            ('Content-Type', 'text/event-stream'),
            ('Cache-Control', 'no-cache'),
            ('X-Accel-Buffering', 'no'),  # disable nginx buffering for SSE
            ('Connection', 'keep-alive'),
        ]
        return Response(generate(), headers=headers, direct_passthrough=True)

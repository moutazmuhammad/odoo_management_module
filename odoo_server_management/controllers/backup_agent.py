import re
import math
import logging

from odoo import http, fields
from odoo.http import request

_logger = logging.getLogger(__name__)

SAFE = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._-]*$')

# Objects under 4 GiB use a single pre-signed PUT; larger ones use multipart.
_SINGLE_LIMIT = 4 * 1024 ** 3
_PART_SIZE = 512 * 1024 ** 2


def _sanitize_seg(seg):
    """A safe single path segment (domain or ip-seg) — no slashes/traversal."""
    seg = (seg or '').strip().strip('/')
    if not seg or '/' in seg or '..' in seg or not SAFE.match(seg):
        return ''
    return seg


class BackupAgentController(http.Controller):
    """Presign service for the per-server backup agents. Each managed server runs
    a local cron agent that detects its exposed DBs, asks here for short-lived
    upload URLs, uploads directly to the Space, then reports back. The Spaces key
    NEVER leaves the manager; the agent only ever holds a throwaway URL during the
    run, plus a low-privilege per-host token that can only mint URLs under that
    host's own prefix."""

    def _host_for_token(self, token):
        token = (token or '').strip()
        if not token or len(token) < 16:
            return None
        return request.env['server.host'].sudo().search(
            [('agent_token', '=', token)], limit=1) or None

    @http.route('/server_backup/agent/presign', type='json', auth='public',
                methods=['POST'], csrf=False)
    def presign(self, token=None, dbs=None, **kw):
        host = self._host_for_token(token)
        if not host:
            return {'error': 'invalid token'}
        Storage = request.env['server.backup.storage'].sudo()
        if not Storage._keys_set():
            return {'error': 'storage not configured'}
        category = host.backup_category or 'odex'
        ip_seg = (host.ip or '').replace('.', '-')
        day = fields.Date.to_string(fields.Date.context_today(host))
        ICP = request.env['ir.config_parameter'].sudo()
        try:
            single_limit = int(ICP.get_param('server.backup.single_limit_bytes') or _SINGLE_LIMIT)
        except (TypeError, ValueError):
            single_limit = _SINGLE_LIMIT
        try:
            part_size = int(ICP.get_param('server.backup.part_size_bytes') or _PART_SIZE)
        except (TypeError, ValueError):
            part_size = _PART_SIZE
        targets = {}
        for d in (dbs or []):
            db = (d.get('db') or '').strip()
            if not db or not SAFE.match(db):
                continue
            seg = _sanitize_seg(d.get('domain')) or ip_seg
            key = Storage._object_key([category, seg, db, '%s_%s.zip' % (db, day)])
            size = int(d.get('size') or 0)
            try:
                if size < single_limit:
                    targets[db] = {'mode': 'single', 'key': key,
                                   'url': Storage._presign_put(key, ttl=12 * 3600)}
                else:
                    upload_id = Storage._create_multipart(key)
                    nparts = min(10000, math.ceil(size / part_size) + 5)
                    targets[db] = {
                        'mode': 'multipart', 'key': key, 'upload_id': upload_id,
                        'part_size': part_size,
                        'part_urls': [Storage._presign_part(key, upload_id, i + 1)
                                      for i in range(nparts)]}
            except Exception:  # noqa: BLE001
                _logger.exception("presign failed for %s/%s", host.name, db)
        _logger.info("Agent presign for host %s: %s db(s)", host.name, len(targets))
        return {'targets': targets}

    @http.route('/server_backup/agent/finalize', type='json', auth='public',
                methods=['POST'], csrf=False)
    def finalize(self, token=None, results=None, done=False, **kw):
        """Called once PER DATABASE (complete/abort its multipart), and once more
        at the end with done=True to prune old objects + stamp last_backup."""
        host = self._host_for_token(token)
        if not host:
            return {'error': 'invalid token'}
        Storage = request.env['server.backup.storage'].sudo()
        ok = 0
        for db, res in (results or {}).items():
            if not isinstance(res, dict):
                continue
            key, upload_id = res.get('key'), res.get('upload_id')
            if res.get('ok'):
                if res.get('mode') == 'multipart' and key and upload_id:
                    try:
                        Storage._complete_multipart(key, upload_id, res.get('parts') or [])
                    except Exception:  # noqa: BLE001
                        _logger.exception("complete multipart failed %s/%s", host.name, db)
                        Storage._abort_multipart(key, upload_id)
                        continue
                ok += 1
                host.sudo().last_backup = fields.Datetime.now()
            elif res.get('mode') == 'multipart' and key and upload_id:
                Storage._abort_multipart(key, upload_id)
        # Final pass: prune old objects for this host's folder.
        if done:
            category = host.backup_category or 'odex'
            ip_seg = (host.ip or '').replace('.', '-')
            try:
                Storage._prune(Storage._object_key([category, ip_seg]) + '/')
            except Exception:  # noqa: BLE001
                _logger.exception("agent prune failed for host %s", host.name)
            host.sudo().last_backup = fields.Datetime.now()
        return {'ok': ok}

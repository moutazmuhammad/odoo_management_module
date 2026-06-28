import re
import logging

from odoo import models, fields, api, _
from odoo.exceptions import UserError, AccessError

from .stage import GROUP_USER, GROUP_OPERATOR

_logger = logging.getLogger(__name__)


class BackupFile(models.TransientModel):
    """Browsable listing of the objects stored in the shared backup Space, one
    row per stored backup, grouped by server/stage. Rows are listed live from
    object storage (not persisted). Download yields a short-lived pre-signed link.

    Access: any internal user may list and download backups, EXCEPT backups that
    belong to a CLIENT server (a stage flagged 'Client Server') — those can only
    be downloaded by Operators/Admins.
    """
    _name = 'server.backup.file'
    _description = 'Stored Backup File'
    _order = 'category, server_name, server_seg, db, last_modified desc'

    category = fields.Char(string='Category', readonly=True)
    server_name = fields.Char(string='Server', readonly=True)
    server_seg = fields.Char(string='Instance (IP/Domain)', readonly=True)
    db = fields.Char(string='Database', readonly=True)
    filename = fields.Char(string='File', readonly=True)
    key = fields.Char(string='Object Key', readonly=True)
    size = fields.Integer(string='Bytes', readonly=True)
    size_human = fields.Char(string='Size', compute='_compute_size_human')
    last_modified = fields.Datetime(string='Created', readonly=True)
    kind = fields.Selection([('daily', 'Daily'), ('manual', 'Manual')],
                            string='Type', readonly=True)
    is_client = fields.Boolean(string='Client Server', readonly=True)
    stage_id = fields.Many2one('server.stage', string='Stage', ondelete='cascade')

    @api.depends('size')
    def _compute_size_human(self):
        for rec in self:
            n = float(rec.size or 0)
            unit = 'B'
            for u in ('B', 'KB', 'MB', 'GB', 'TB'):
                unit = u
                if n < 1024.0:
                    break
                n /= 1024.0
            rec.size_human = ('%d B' % (rec.size or 0)) if unit == 'B' else '%.1f %s' % (n, unit)

    # ------------------------------------------------------------------
    # Client-server classification (download gating)
    # ------------------------------------------------------------------
    @api.model
    def _norm_seg(self, name):
        """The ip/domain path segment for a stage's name — KEEPS dots (matching the
        dotted ip/domain segments used in backup keys). Strips any URL scheme/port."""
        if not name:
            return ''
        h = name.strip().lower()
        m = re.match(r'^\s*https?://([^/:]+)', h)
        if m:
            h = m.group(1)
        h = h.split('/')[0].split(':')[0]
        return self.env['server.host']._backup_host_seg(h)

    @api.model
    def _client_dbs_segs(self):
        """DB names and server segments that belong to CLIENT stages."""
        stages = self.env['server.stage'].sudo().search([('client_stage', '=', True)])
        dbs, segs = set(), set()
        for s in stages:
            for d in (s.available_databases or '').splitlines():
                if d.strip():
                    dbs.add(d.strip())
            seg = self._norm_seg(s.name)
            if seg:
                segs.add(seg)
        return dbs, segs

    def _is_client_key(self, key):
        """Re-derive the client flag from the key at download time (never trust a
        stored flag the user could have crafted)."""
        rec = self._parse_key(key, self.env['server.backup.storage']._prefix())
        if not rec:
            return True  # unknown layout -> treat as sensitive
        dbs, segs = self._client_dbs_segs()
        return rec['db'] in dbs or rec['server_seg'] in segs

    # ------------------------------------------------------------------
    # Key parsing
    # ------------------------------------------------------------------
    @api.model
    def _parse_key(self, key, prefix):
        k = key
        if prefix:
            p = prefix.strip('/') + '/'
            if k.startswith(p):
                k = k[len(p):]
        parts = [x for x in k.split('/') if x]
        if not parts or parts[0] == '_status':
            return None
        if parts[0] == 'manual':
            if len(parts) < 4:
                return None
            return {'category': parts[1], 'server_name': '', 'server_seg': parts[2],
                    'db': parts[-1].rsplit('.', 1)[0], 'filename': parts[-1],
                    'kind': 'manual'}
        # Daily layout (length-agnostic so both are read):
        #   new:    <category>/<server>/<domain>/<db>/<db>_<date>.zip   (>= 5 parts)
        #   legacy: <category>/<domain>/<db>/<db>_<date>.zip            (== 4 parts)
        # db is always parts[-2], filename parts[-1], domain parts[-3]; the server
        # name segment exists only in the new layout (parts[1]).
        if len(parts) < 4:
            return None
        return {'category': parts[0],
                'server_name': parts[1] if len(parts) >= 5 else '',
                'server_seg': parts[-3], 'db': parts[-2],
                'filename': parts[-1], 'kind': 'daily'}

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------
    @api.model
    def action_refresh(self):
        """List the bucket and (re)build this user's rows, then open the tree."""
        self.env['server.stage']._check_access(GROUP_USER)
        Storage = self.env['server.backup.storage']
        if not Storage._keys_set():
            raise UserError(_("Backup storage is not configured "
                              "(General Settings → Backups)."))
        self.sudo().search([('create_uid', '=', self.env.uid)]).unlink()
        client_dbs, client_segs = self._client_dbs_segs()
        prefix = Storage._prefix()
        cli = Storage._boto_client()
        bucket = Storage._bucket()
        token, vals = None, []
        while True:
            kw = {'Bucket': bucket}
            if prefix:
                kw['Prefix'] = prefix.strip('/') + '/'
            if token:
                kw['ContinuationToken'] = token
            resp = cli.list_objects_v2(**kw)
            for o in resp.get('Contents', []):
                rec = self._parse_key(o['Key'], prefix)
                if not rec:
                    continue
                lm = o.get('LastModified')
                rec.update({
                    'key': o['Key'],
                    'size': int(o.get('Size') or 0),
                    'last_modified': lm.replace(tzinfo=None) if lm else False,
                    'is_client': rec['db'] in client_dbs or rec['server_seg'] in client_segs,
                })
                vals.append(rec)
            if resp.get('IsTruncated'):
                token = resp.get('NextContinuationToken')
            else:
                break
        if vals:
            self.create(vals)
        return {
            'type': 'ir.actions.act_window',
            'name': _('Stored Backups'),
            'res_model': 'server.backup.file',
            'view_mode': 'tree',
            'target': 'current',
            'domain': [('create_uid', '=', self.env.uid)],
            'context': {'search_default_group_server': 1},
        }

    @api.model
    def _vals_for_stage(self, stage):
        """Backup rows (dicts) belonging to one stage, read live from the Space.
        A backup belongs to the stage if its DB is one of the stage's databases,
        or its server segment matches the stage's domain."""
        Storage = self.env['server.backup.storage']
        if not Storage._keys_set() or not stage:
            return []
        category = stage.host_id.backup_category or 'odex'
        stage_dbs = {d.strip() for d in (stage.available_databases or '').splitlines()
                     if d.strip()}
        seg = self._norm_seg(stage.name)
        # Match by the stage's segment ONLY when it is a real domain. The bare
        # server IP is shared by every instance that has no domain, so matching
        # on it would wrongly show all those DBs under each IP-named stage — for
        # those, match strictly by the stage's own databases.
        ip_seg = self.env['server.host']._backup_host_seg(stage.host_id.ip)
        domain_seg = seg if (seg and seg != ip_seg) else None
        prefix = Storage._prefix()
        client_dbs, client_segs = self._client_dbs_segs()
        cli = Storage._boto_client()
        bucket = Storage._bucket()
        pref = (prefix.strip('/') + '/') if prefix else ''
        # Only the DAILY backups area — manual backups are one-time downloads and
        # are deliberately excluded from the stage's Backups list.
        bases = ['%s%s/' % (pref, category)]
        vals = []
        for base in bases:
            token = None
            while True:
                kw = {'Bucket': bucket, 'Prefix': base}
                if token:
                    kw['ContinuationToken'] = token
                resp = cli.list_objects_v2(**kw)
                for o in resp.get('Contents', []):
                    rec = self._parse_key(o['Key'], prefix)
                    if not rec:
                        continue
                    if not ((stage_dbs and rec['db'] in stage_dbs)
                            or (domain_seg and rec['server_seg'] == domain_seg)):
                        continue
                    lm = o.get('LastModified')
                    rec.update({
                        'key': o['Key'], 'size': int(o.get('Size') or 0),
                        'last_modified': lm.replace(tzinfo=None) if lm else False,
                        'is_client': rec['db'] in client_dbs or rec['server_seg'] in client_segs,
                        'stage_id': stage.id,
                    })
                    vals.append(rec)
                if resp.get('IsTruncated'):
                    token = resp.get('NextContinuationToken')
                else:
                    break
        return vals

    @api.model
    def _populate_for_stage(self, stage):
        """Replace this stage's transient backup rows with a fresh listing from
        the object Space and RETURN the records. Real records (not NewId) so the
        form renders them."""
        self.sudo().search([('stage_id', '=', stage.id)]).unlink()
        vals = self._vals_for_stage(stage)
        return self.create(vals) if vals else self.browse()

    def action_download(self):
        self.ensure_one()
        # Client-server backups: Operators/Admins only. Re-check from the key.
        if self._is_client_key(self.key):
            self.env['server.stage']._check_access(GROUP_OPERATOR)
        else:
            self.env['server.stage']._check_access(GROUP_USER)
        url = self.env['server.backup.storage']._presign_get(
            self.key, filename=self.filename)
        return {'type': 'ir.actions.act_url', 'url': url, 'target': 'self'}

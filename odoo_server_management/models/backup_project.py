import logging

from odoo import models, fields, api, _
from odoo.exceptions import UserError

from .stage import GROUP_ADMIN, GROUP_OPERATOR

_logger = logging.getLogger(__name__)


class BackupProject(models.Model):
    """A DigitalOcean project / S3-compatible Spaces target with its own
    credentials and bucket. Servers are assigned to a project; the daily backup
    job uploads each of a server's databases to that project's bucket using a
    short-lived pre-signed PUT URL — the keys never leave Odoo and are never
    stored on the managed servers."""
    _name = 'server.backup.project'
    _description = 'Backup Project (object storage target)'
    _order = 'name'

    name = fields.Char(string='Project Name', required=True)
    region = fields.Char(
        string='Region', required=True, default='nyc3',
        help="Spaces region, e.g. nyc3 / fra1 / sgp1. The endpoint is "
             "https://<region>.digitaloceanspaces.com.")
    endpoint = fields.Char(
        string='Custom Endpoint',
        help="Optional. Override the endpoint URL (e.g. for non-DigitalOcean "
             "S3). Leave empty to derive it from the region.")
    bucket = fields.Char(string='Bucket / Space', required=True)
    prefix = fields.Char(
        string='Key Prefix', default='',
        help="Optional folder prefix prepended to every object key.")
    retention_days = fields.Integer(
        string='Retention (days)', default=7,
        help="Daily objects older than this are pruned after each run.")
    daily_backup_enabled = fields.Boolean(string='Daily Backups Enabled', default=True)

    # Encrypted-at-rest, write-only credentials (mirrors the settings/token
    # pattern): the plaintext is never echoed back to the form — blank on read,
    # type a value to (re)set it, leave blank to keep the stored one.
    access_key_enc = fields.Char(string='Access Key (encrypted)', groups=GROUP_ADMIN)
    secret_key_enc = fields.Char(string='Secret Key (encrypted)', groups=GROUP_ADMIN)
    access_key = fields.Char(
        string='Access Key', store=False, groups=GROUP_ADMIN,
        compute='_compute_secrets', inverse='_inverse_access_key')
    secret_key = fields.Char(
        string='Secret Key', store=False, groups=GROUP_ADMIN,
        compute='_compute_secrets', inverse='_inverse_secret_key')
    keys_set = fields.Boolean(string='Credentials Configured',
                              compute='_compute_keys_set')

    host_ids = fields.One2many('server.host', 'backup_project_id',
                               string='Servers')
    host_count = fields.Integer(compute='_compute_host_count')

    _sql_constraints = [
        ('unique_project_name', 'unique(name)', 'Project name must be unique!'),
    ]

    @api.depends('host_ids')
    def _compute_host_count(self):
        for rec in self:
            rec.host_count = len(rec.host_ids)

    def _compute_secrets(self):
        # Never echo secrets back to the UI.
        for rec in self:
            rec.access_key = ''
            rec.secret_key = ''

    @api.depends('access_key_enc', 'secret_key_enc')
    def _compute_keys_set(self):
        for rec in self:
            rec.keys_set = bool(rec.access_key_enc and rec.secret_key_enc)

    def _inverse_access_key(self):
        Stage = self.env['server.stage']
        for rec in self:
            if rec.access_key:
                rec.access_key_enc = Stage._encrypt_secret(rec.access_key.strip())

    def _inverse_secret_key(self):
        Stage = self.env['server.stage']
        for rec in self:
            if rec.secret_key:
                rec.secret_key_enc = Stage._encrypt_secret(rec.secret_key.strip())

    # ------------------------------------------------------------------
    # Internal credential accessors (decrypt on demand; never exposed in views)
    # ------------------------------------------------------------------
    def _access_key(self):
        self.ensure_one()
        return self.env['server.stage']._decrypt_secret(self.sudo().access_key_enc)

    def _secret_key(self):
        self.ensure_one()
        return self.env['server.stage']._decrypt_secret(self.sudo().secret_key_enc)

    def _endpoint_url(self):
        self.ensure_one()
        if self.endpoint:
            return self.endpoint.rstrip('/')
        return 'https://%s.digitaloceanspaces.com' % (self.region or 'nyc3').strip()

    def _boto_client(self):
        """Build an S3 client for this project. Requires boto3 on the Odoo host."""
        self.ensure_one()
        try:
            import boto3  # noqa: E402
            from botocore.config import Config  # noqa: E402
        except ImportError:
            raise UserError(_(
                "boto3 is not installed on the Odoo host. Install it to use "
                "per-project backups:  pip3 install boto3"))
        ak, sk = self._access_key(), self._secret_key()
        if not (ak and sk):
            raise UserError(_("Project '%s' has no access/secret key set.") % self.name)
        return boto3.client(
            's3', region_name=(self.region or 'nyc3').strip(),
            endpoint_url=self._endpoint_url(),
            aws_access_key_id=ak, aws_secret_access_key=sk,
            config=Config(signature_version='s3v4', retries={'max_attempts': 3}))

    def _object_key(self, parts):
        """Join key parts with the optional project prefix."""
        self.ensure_one()
        segs = [s for s in ([self.prefix] + list(parts)) if s]
        return '/'.join(p.strip('/') for p in segs)

    def _presign_put(self, object_key, ttl=3600):
        """Return a short-lived pre-signed HTTP PUT URL for `object_key` (single
        upload, used for objects under ~5 GB)."""
        return self._boto_client().generate_presigned_url(
            'put_object', Params={'Bucket': self.bucket, 'Key': object_key},
            ExpiresIn=ttl)

    # --- Multipart upload (large objects; pre-signed, no creds on server) -----
    def _create_multipart(self, object_key):
        r = self._boto_client().create_multipart_upload(
            Bucket=self.bucket, Key=object_key)
        return r['UploadId']

    def _presign_part(self, object_key, upload_id, part_number, ttl=43200):
        return self._boto_client().generate_presigned_url(
            'upload_part',
            Params={'Bucket': self.bucket, 'Key': object_key,
                    'UploadId': upload_id, 'PartNumber': int(part_number)},
            ExpiresIn=ttl)

    def _complete_multipart(self, object_key, upload_id, parts):
        """parts = [{'ETag':..,'PartNumber':..}, ...] (any order)."""
        ordered = sorted(
            ({'ETag': p['ETag'], 'PartNumber': int(p['PartNumber'])} for p in parts),
            key=lambda p: p['PartNumber'])
        return self._boto_client().complete_multipart_upload(
            Bucket=self.bucket, Key=object_key, UploadId=upload_id,
            MultipartUpload={'Parts': ordered})

    def _abort_multipart(self, object_key, upload_id):
        try:
            self._boto_client().abort_multipart_upload(
                Bucket=self.bucket, Key=object_key, UploadId=upload_id)
        except Exception:  # noqa: BLE001
            _logger.exception("Abort multipart failed for %s", object_key)

    def _prune(self, key_prefix, retention_days):
        """Delete objects under `key_prefix` older than `retention_days`."""
        self.ensure_one()
        if not retention_days or retention_days <= 0:
            return 0
        import datetime
        cli = self._boto_client()
        cutoff_days = retention_days
        deleted = 0
        token = None
        # Compare against object age via LastModified (tz-aware UTC).
        now = fields.Datetime.now()
        while True:
            kw = {'Bucket': self.bucket, 'Prefix': key_prefix}
            if token:
                kw['ContinuationToken'] = token
            resp = cli.list_objects_v2(**kw)
            old = []
            for obj in resp.get('Contents', []):
                lm = obj['LastModified']
                # LastModified is tz-aware; make `now` comparable.
                age = (datetime.datetime.now(lm.tzinfo) - lm).days
                if age > cutoff_days:
                    old.append({'Key': obj['Key']})
            if old:
                cli.delete_objects(Bucket=self.bucket, Delete={'Objects': old})
                deleted += len(old)
            if resp.get('IsTruncated'):
                token = resp.get('NextContinuationToken')
            else:
                break
        return deleted

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------
    def action_test_storage(self):
        """Verify credentials + bucket + region by HEAD-ing the bucket."""
        self.env['server.stage']._check_access(GROUP_ADMIN)
        self.ensure_one()
        bucket = (self.bucket or '').strip()
        try:
            self._boto_client().head_bucket(Bucket=bucket)
        except UserError:
            raise
        except Exception as exc:  # noqa: BLE001
            resp = getattr(exc, 'response', None) or {}
            err = resp.get('Error') or {}
            meta = resp.get('ResponseMetadata') or {}
            code = err.get('Code')
            status = meta.get('HTTPStatusCode')
            region_hdr = (meta.get('HTTPHeaders') or {}).get('x-amz-bucket-region')
            if region_hdr and region_hdr != (self.region or '').strip():
                hint = _(" — the bucket is in region '%s', not '%s'. Set Region to "
                         "'%s'.") % (region_hdr, self.region, region_hdr)
            elif code in ('404', 'NoSuchBucket', 'NoSuchKey') or status == 404:
                hint = _(" — bucket '%s' was not found at %s. Check the exact "
                         "bucket/Space name and the Region.") % (bucket, self._endpoint_url())
            elif code in ('403', 'AccessDenied', 'SignatureDoesNotMatch',
                          'InvalidAccessKeyId') or status in (401, 403):
                hint = _(" — credentials rejected. Re-enter the access/secret key.")
            else:
                hint = ''
            raise UserError(_("❌ Storage check failed [%s]: %s%s")
                            % (code or status or 'error', exc, hint))
        return self.env['server.stage']._notify(
            _("✅ Connected to bucket '%s' (%s).") % (bucket, self.region))

    def action_run_now(self):
        """Run the daily backup immediately for all servers in this project."""
        self.env['server.stage']._check_access(GROUP_OPERATOR)
        self.ensure_one()
        hosts = self.host_ids
        if not hosts:
            raise UserError(_("No servers are assigned to project '%s'.") % self.name)
        total = 0
        for host in hosts:
            total += host._run_daily_backup(self)
        return self.env['server.stage']._notify(
            _("✅ Backup run complete: %s database(s) across %s server(s).")
            % (total, len(hosts)))

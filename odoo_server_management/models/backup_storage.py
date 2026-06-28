import logging

from odoo import models, fields, api, _
from odoo.exceptions import UserError

from .stage import GROUP_ADMIN

_logger = logging.getLogger(__name__)


class BackupStorage(models.AbstractModel):
    """Single, global object-storage target (one S3/Spaces bucket shared by every
    server). Bucket, region and credentials live in ir.config_parameter and are
    edited in General Settings — there is exactly one Space for all backups.

    The daily job and the real-time button upload each database with a
    short-lived pre-signed URL minted here, so the access/secret keys never leave
    Odoo and are never stored on the managed servers. The object key layout is:

        <prefix?>/<category>/<server>/<ip-or-domain>/<db>/<db>_<date>.zip  (daily)
        <prefix?>/manual/<category>/<ip-or-domain>/<db>.<ext>             (real-time)
    """
    _name = 'server.backup.storage'
    _description = 'Backup Storage (global object-storage target)'

    # ------------------------------------------------------------------
    # Config accessors (all read the single global ir.config_parameter set)
    # ------------------------------------------------------------------
    @api.model
    def _cfg(self, key, default=''):
        return self.env['ir.config_parameter'].sudo().get_param(key, default=default)

    @api.model
    def _bucket(self):
        return (self._cfg('server.backup.bucket') or '').strip()

    @api.model
    def _region(self):
        return (self._cfg('server.backup.region', 'fra1') or 'fra1').strip()

    @api.model
    def _prefix(self):
        return (self._cfg('server.backup.prefix') or '').strip()

    @api.model
    def _endpoint_url(self):
        ep = (self._cfg('server.backup.endpoint') or '').strip()
        if ep:
            return ep.rstrip('/')
        return 'https://%s.digitaloceanspaces.com' % self._region()

    @api.model
    def _int_cfg(self, key, default):
        try:
            return int(self._cfg(key, str(default)) or default)
        except (TypeError, ValueError):
            return default

    @api.model
    def _retention_days(self):
        return self._int_cfg('server.backup.retention_days', 7)

    @api.model
    def _signed_url_ttl(self):
        return self._int_cfg('server.backup.signed_url_ttl', 3600)

    @api.model
    def _daily_enabled(self):
        return (self._cfg('server.backup.daily_enabled', '1') or '1') not in ('0', 'False', 'false', '')

    @api.model
    def _access_key(self):
        return self.env['server.stage']._get_secret_param('server.backup.access_key')

    @api.model
    def _secret_key(self):
        return self.env['server.stage']._get_secret_param('server.backup.secret_key')

    @api.model
    def _keys_set(self):
        return bool(self._access_key() and self._secret_key() and self._bucket())

    # ------------------------------------------------------------------
    # S3 client + key helpers
    # ------------------------------------------------------------------
    @api.model
    def _boto_client(self):
        """Build an S3 client for the global Space. Requires boto3 on the Odoo host."""
        try:
            import boto3  # noqa: E402
            from botocore.config import Config  # noqa: E402
        except ImportError:
            raise UserError(_(
                "boto3 is not installed on the Odoo host. Install it to use "
                "backups:  pip3 install boto3"))
        ak, sk = self._access_key(), self._secret_key()
        if not (ak and sk):
            raise UserError(_("No backup access/secret key is configured "
                              "(General Settings → Backups)."))
        return boto3.client(
            's3', region_name=self._region(),
            endpoint_url=self._endpoint_url(),
            aws_access_key_id=ak, aws_secret_access_key=sk,
            config=Config(signature_version='s3v4', retries={'max_attempts': 3}))

    @api.model
    def _object_key(self, parts):
        """Join key parts with the optional global prefix."""
        segs = [s for s in ([self._prefix()] + list(parts)) if s]
        return '/'.join(p.strip('/') for p in segs)

    @api.model
    def _presign_put(self, object_key, ttl=3600):
        return self._boto_client().generate_presigned_url(
            'put_object', Params={'Bucket': self._bucket(), 'Key': object_key},
            ExpiresIn=ttl)

    @api.model
    def _presign_get(self, object_key, ttl=None, filename=None):
        params = {'Bucket': self._bucket(), 'Key': object_key}
        if filename:
            params['ResponseContentDisposition'] = 'attachment; filename="%s"' % filename
        return self._boto_client().generate_presigned_url(
            'get_object', Params=params, ExpiresIn=ttl or self._signed_url_ttl())

    # --- Multipart upload (large objects; pre-signed, no creds on server) -----
    @api.model
    def _create_multipart(self, object_key):
        r = self._boto_client().create_multipart_upload(
            Bucket=self._bucket(), Key=object_key)
        return r['UploadId']

    @api.model
    def _presign_part(self, object_key, upload_id, part_number, ttl=43200):
        return self._boto_client().generate_presigned_url(
            'upload_part',
            Params={'Bucket': self._bucket(), 'Key': object_key,
                    'UploadId': upload_id, 'PartNumber': int(part_number)},
            ExpiresIn=ttl)

    @api.model
    def _complete_multipart(self, object_key, upload_id, parts):
        ordered = sorted(
            ({'ETag': p['ETag'], 'PartNumber': int(p['PartNumber'])} for p in parts),
            key=lambda p: p['PartNumber'])
        return self._boto_client().complete_multipart_upload(
            Bucket=self._bucket(), Key=object_key, UploadId=upload_id,
            MultipartUpload={'Parts': ordered})

    @api.model
    def _abort_multipart(self, object_key, upload_id):
        try:
            self._boto_client().abort_multipart_upload(
                Bucket=self._bucket(), Key=object_key, UploadId=upload_id)
        except Exception:  # noqa: BLE001
            _logger.exception("Abort multipart failed for %s", object_key)

    # ------------------------------------------------------------------
    # Pruning / purging
    # ------------------------------------------------------------------
    @api.model
    def _purge_prefix(self, key_prefix):
        """Delete ALL objects under `key_prefix` (regardless of age)."""
        cli = self._boto_client()
        bucket = self._bucket()
        deleted, token = 0, None
        while True:
            kw = {'Bucket': bucket, 'Prefix': key_prefix}
            if token:
                kw['ContinuationToken'] = token
            resp = cli.list_objects_v2(**kw)
            objs = [{'Key': o['Key']} for o in resp.get('Contents', [])]
            if objs:
                cli.delete_objects(Bucket=bucket, Delete={'Objects': objs})
                deleted += len(objs)
            if resp.get('IsTruncated'):
                token = resp.get('NextContinuationToken')
            else:
                break
        return deleted

    @api.model
    def _prune(self, key_prefix, retention_days=None):
        """Delete objects under `key_prefix` older than retention_days."""
        retention_days = self._retention_days() if retention_days is None else retention_days
        if not retention_days or retention_days <= 0:
            return 0
        import datetime
        cli = self._boto_client()
        bucket = self._bucket()
        deleted, token = 0, None
        while True:
            kw = {'Bucket': bucket, 'Prefix': key_prefix}
            if token:
                kw['ContinuationToken'] = token
            resp = cli.list_objects_v2(**kw)
            old = []
            for obj in resp.get('Contents', []):
                lm = obj['LastModified']
                age = (datetime.datetime.now(lm.tzinfo) - lm).days
                if age > retention_days:
                    old.append({'Key': obj['Key']})
            if old:
                cli.delete_objects(Bucket=bucket, Delete={'Objects': old})
                deleted += len(old)
            if resp.get('IsTruncated'):
                token = resp.get('NextContinuationToken')
            else:
                break
        return deleted

    @api.model
    def _cron_purge_manual(self):
        """Daily (03:00): empty the 'manual/' area so on-demand backups never
        accumulate."""
        if not self._keys_set():
            return
        try:
            n = self._purge_prefix(self._object_key(['manual']) + '/')
            if n:
                _logger.info("Purged %s manual backup object(s) from %s",
                             n, self._bucket())
        except Exception:  # noqa: BLE001
            _logger.exception("Manual-backup purge failed")

    # ------------------------------------------------------------------
    # Connectivity test (called from General Settings)
    # ------------------------------------------------------------------
    @api.model
    def action_test_storage(self):
        """Verify access to the TARGET Space directly (bucket-scoped). We do NOT
        call ListBuckets — many valid Spaces keys are scoped to a bucket and lack
        the account-level ListAllMyBuckets permission, yet can read/write their
        Space perfectly. So success = we can list the bucket itself."""
        self.env['server.stage']._check_access(GROUP_ADMIN)
        bucket = self._bucket()
        if not bucket:
            raise UserError(_("No bucket / Space name is configured."))
        cli = self._boto_client()
        try:
            cli.list_objects_v2(Bucket=bucket, MaxKeys=1)
        except Exception as exc:  # noqa: BLE001
            resp = getattr(exc, 'response', None) or {}
            code = (resp.get('Error') or {}).get('Code')
            meta = resp.get('ResponseMetadata') or {}
            region_hdr = (meta.get('HTTPHeaders') or {}).get('x-amz-bucket-region')
            if region_hdr and region_hdr != self._region():
                raise UserError(_(
                    "❌ Space '%s' is in region '%s', not '%s'. Set Region to '%s'.")
                    % (bucket, region_hdr, self._region(), region_hdr))
            if code in ('NoSuchBucket', '404'):
                raise UserError(_(
                    "❌ No Space named '%s' in region '%s'.") % (bucket, self._region()))
            if code in ('AccessDenied', '403', 'InvalidAccessKeyId',
                        'SignatureDoesNotMatch'):
                raise UserError(_(
                    "❌ Access denied to Space '%s' [%s]. Check the access/secret "
                    "key (a DigitalOcean **Spaces key**, not an API token) and "
                    "that it is allowed on this Space.") % (bucket, code))
            raise UserError(_("❌ Could not access Space '%s': %s") % (bucket, exc))
        return self.env['server.stage']._notify(
            _("✅ Connected to Space '%s' (%s).") % (bucket, self._region()))

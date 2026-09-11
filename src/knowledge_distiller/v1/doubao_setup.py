"""Read-only TOS discovery for setup; never upload audio or create resources."""
import json
import re
from uuid import uuid4

from .database import connect


class SetupError(ValueError):
    pass


def valid_location(region, bucket):
    return (isinstance(region, str) and isinstance(bucket, str)
            and re.fullmatch(r'[a-z]{2}-[a-z]+(?:-\d+)?', region)
            and re.fullmatch(r'[a-z0-9][a-z0-9-]{1,61}[a-z0-9]', bucket))


def discover(access_key, secret_key, *, factory=None):
    access_key, secret_key = access_key.strip(), secret_key.strip()
    if not access_key or not secret_key or any(c.isspace() for c in access_key+secret_key):
        raise SetupError('doubao_storage_credentials_invalid')
    if factory is None:
        try:
            from tos import TosClientV2
        except ImportError:
            raise SetupError('doubao_runtime_unavailable') from None
        factory = TosClientV2
    client = None
    try:
        client = factory(access_key, secret_key, 'https://tos-cn-beijing.volces.com', 'cn-beijing',
                         max_retry_count=0, connection_time=5, socket_timeout=15, request_timeout=20)
        result = client.list_buckets()
        buckets = []
        for bucket in result.buckets:
            if not valid_location(bucket.location, bucket.name):
                raise SetupError('doubao_storage_response_invalid')
            buckets.append({'name': bucket.name, 'region': bucket.location})
        return sorted(buckets, key=lambda b: (b['name'], b['region']))
    except SetupError:
        raise
    except Exception as error:
        code = getattr(error, 'code', '')
        if code in {'InvalidAccessKeyId', 'SignatureDoesNotMatch', 'InvalidSecurityToken'}:
            reason = 'doubao_storage_credentials_invalid'
        elif code in {'AccessDenied', 'Forbidden'} or getattr(error, 'status_code', None) == 403:
            reason = 'doubao_storage_list_denied'
        elif code == 'RequestTimeTooSkewed':
            reason = 'doubao_storage_clock_invalid'
        else:
            reason = 'doubao_storage_unavailable'
        raise SetupError(reason) from None
    finally:
        if client is not None:
            client.close()


def save_discovery(service, access_key='', secret_key=''):
    if not access_key and not secret_key:
        account = service.store.setting('seed_draft_tos_account')
        try:
            saved = json.loads(service.keychain_factory(account).load()) if account else {}
            access_key, secret_key = saved['access_key'], saved['secret_key']
        except Exception:
            raise SetupError('doubao_storage_credentials_invalid') from None
    buckets = discover(access_key, secret_key)
    result = {'id': uuid4().hex, 'buckets': buckets}
    service._save_draft_secret('seed_draft_tos_account',
        json.dumps({'access_key':access_key.strip(), 'secret_key':secret_key.strip()}),
        {'seed_storage_discovery':json.dumps(result), 'seed_draft_region':'', 'seed_draft_bucket':''})
    return bool(buckets)


def select_bucket(store, discovery_id, name):
    # Old pages cannot apply a bucket list obtained using different credentials.
    with connect(store.path) as db:
        db.execute('BEGIN IMMEDIATE')
        row = db.execute("SELECT value FROM settings WHERE key='seed_storage_discovery'").fetchone()
        discovery = json.loads(row[0]) if row else {}
        if not discovery_id or discovery.get('id') != discovery_id:
            raise SetupError('doubao_storage_selection_expired')
        bucket = next((b for b in discovery['buckets'] if b['name'] == name), None)
        if bucket is None:
            raise SetupError('doubao_storage_selection_required')
        for key, value in [('seed_draft_region',bucket['region']), ('seed_draft_bucket',bucket['name'])]:
            db.execute('INSERT INTO settings(key,value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value', (key,value))

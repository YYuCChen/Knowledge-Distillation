"""One-time upgrade operation; never imported by normal application startup."""
import json
from pathlib import Path
import sqlite3
from .local_secrets import LocalSecrets, SecretError
from .keychain import KeychainError

ACCOUNT_KEYS = {'llm_secret_account','llm_draft_secret_account','seed_draft_api_account',
                'seed_draft_tos_account','asr_seed_api_account','asr_seed_tos_account'}


def accounts(database):
    # Read the old schema without initializing or upgrading the original database.
    with sqlite3.connect(Path(database).resolve().as_uri()+'?mode=ro',uri=True) as db:
        values=dict(db.execute('SELECT key,value FROM settings'))
        found={value for key,value in values.items() if key in ACCOUNT_KEYS and value}
        for platform,context in db.execute('SELECT platform,browser_context FROM source_connections'):
            if context and context.startswith('owned:'):
                found.add(platform+'-session-'+context[6:])
        for (app_id,) in db.execute('SELECT app_id FROM feishu_binding'):
            found.add('feishu-app-'+app_id)
        pairing=json.loads(values.get('feishu_pairing') or '{}')
        if pairing.get('app_id'):found.add('feishu-app-'+pairing['app_id'])
    return sorted(found)


def migrate(database, destination, *, execute=False, legacy_factory=None):
    selected=accounts(database)
    result={'planned':len(selected),'imported':0,'already_local':0,'failed':0,'executed':execute}
    # Dry run does not touch the Keychain or create a credentials directory.
    if not execute:return result
    target=LocalSecrets(destination)
    for account in selected:
        try:
            status=target.status(account)
            if status in ('validated','pending_validation'):
                result['already_local']+=1
                continue
            if status!='missing':raise SecretError('credential_unavailable')
            target.import_legacy(account,legacy_factory)
            result['imported']+=1
        except (KeychainError,OSError):
            # Only aggregate outcomes: no values, account identifiers or exception text.
            result['failed']+=1
    return result

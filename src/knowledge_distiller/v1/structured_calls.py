"""One request/validation contract and bounded diagnostic files for organization."""
import hashlib
import json
import logging
import os
import time
from dataclasses import replace
from pathlib import Path

from jsonschema import Draft202012Validator, ValidationError
from .llm import OpenAIResponsesClient, LLMRequestError

logger = logging.getLogger(__name__)


class StructuredCalls:
    def __init__(self, client, directory=None):
        self.client = client
        self.directory = Path(directory) if directory else None
        self.records = {}

    def complete(self, stage, system, payload, schema, max_tokens):
        validator = Draft202012Validator(schema)
        identity = {'stage': stage, 'system': system, 'input': payload, 'schema': schema,
            'model': self.client.model, 'endpoint': self.client.base_url,
            'effort': getattr(self.client, 'reasoning_effort', getattr(self.client, 'effort', None)),
            'service_tier': getattr(self.client, 'service_tier', None), 'max_tokens': max_tokens}
        fingerprint = hashlib.sha256(json.dumps(identity, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        path = self.directory / f'{stage}.json' if self.directory else None
        if path and path.is_file() and not path.is_symlink():
            try:
                saved = json.loads(path.read_text())
                if saved.get('fingerprint') == fingerprint and saved.get('accepted'):
                    value = _response_value(saved['text'], schema)
                    validator.validate(value)
                    self.records[stage] = saved
                    return value
            except (ValueError, OSError, ValidationError, KeyError, TypeError):
                pass
        client = self.client
        if isinstance(client, OpenAIResponsesClient):
            client = replace(client, text_format={'type': 'json_schema', 'name': stage,
                'strict': True, 'schema': schema})
        else:
            system += '\n输出必须满足此JSON Schema：\n' + json.dumps(schema, ensure_ascii=False)
        started = time.monotonic()
        record = {'fingerprint': fingerprint, 'request': identity, 'accepted': False}
        self.records[stage] = record
        try:
            text = client.complete(system=system, user=json.dumps(payload, ensure_ascii=False), max_tokens=max_tokens)
            record['text'] = text
            value = _response_value(text, schema)
            validator.validate(value)
            record['validation'] = 'structure_valid'
            return value
        except (ValueError, ValidationError) as error:
            record['validation'] = 'invalid_json' if isinstance(error, ValueError) else 'schema_invalid'
            if isinstance(error, ValidationError):
                record['error_path'] = list(error.absolute_path)
                record['validator'] = error.validator
            logger.warning('Organization %s response failed %s', stage, record['validation'])
            raise LLMRequestError('llm_response_invalid') from error
        except LLMRequestError as error:
            record['validation'] = 'provider_failed'
            raise
        finally:
            record['seconds'] = round(time.monotonic() - started, 3)
            self.save(stage)

    def accept(self, stage):
        if stage in self.records:
            self.records[stage]['accepted'] = True
            self.save(stage)

    def save(self, stage):
        if not self.directory:
            return
        try:
            self.directory.mkdir(parents=True, exist_ok=True)
            target = self.directory / f'{stage}.json'
            temp = target.with_suffix('.tmp')
            fd = os.open(temp, os.O_CREAT | os.O_TRUNC | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, 'w') as output:
                json.dump(self.records[stage], output, ensure_ascii=False)
            os.replace(temp, target)
        except OSError:
            logger.warning('Organization %s diagnostic could not be saved', stage)


def _response_value(text, schema):
    raw = text.strip()
    if raw.startswith('```json\n') and raw.endswith('\n```'):
        raw = raw[len('```json\n'):-len('\n```')]
    value = json.loads(raw)
    # Some compatible transports echo the exact supplied schema beside the
    # instance. Only remove a provably identical envelope; never drop unknown
    # model fields or repair a domain decision.
    if (isinstance(value, dict) and not set(schema) & set(schema.get('properties', {}))
            and set(value) == set(schema) | set(schema.get('properties', {}))
            and all(value[key] == expected for key, expected in schema.items())):
        value = {key: value[key] for key in schema['properties']}
    return value

"""Request-owned model responses. A prepared response is never a DB commit.

Only explicit current pointers are read. Orphan response files are evidence, not
recovery candidates. Callers still run their current domain validators.
"""
from contextlib import contextmanager
import hashlib
import json
from pathlib import Path
import re
import uuid

from .file_lock import acquire
from .local_records import write_record
from .windows_platform import filesystem_path
from .model_json import PARSER_VERSION, parse_model_json, ModelJSONError


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def text_hash(text):
    return hashlib.sha256(text.encode()).hexdigest()


class ResponseReceipts:
    def __init__(self, root, *, operation, source, contract, model_identity=None,
                 source_version_id=None, requested_fields=(), parent_response_hash=None,
                 validator_version='1'):
        # Lexical conversion preserves symlink checks and the existing physical scope.
        self.root = filesystem_path(root) if root is not None else None
        self.identity = dict(operation=operation, source_hash=text_hash(source),
            source_version_id=source_version_id or text_hash(source),
            request_contract_hash=digest(contract),
            model_config_identity_without_secret=digest(model_identity),
            requested_fields=list(requested_fields), parent_response_hash=parent_response_hash)
        self.validator_version = validator_version
        self.last_diagnostic = None
        self.last_state = None
        self.last_request_id = None
        self._memory = {}

    @contextmanager
    def locked(self):
        if self.root is None:
            yield
            return
        self.root.mkdir(parents=True, exist_ok=True)
        if self.root.is_symlink() or (self.root / '.lock').is_symlink():
            raise OSError('response_checkpoint_unsafe')
        handle = acquire(self.root / '.lock')
        try:
            yield
        finally:
            handle.close()

    def _read(self, name):
        if self.root is None:
            return self._memory.get(name)
        path = self.root / name
        if path.is_symlink() or self.root.is_symlink():
            raise OSError('response_checkpoint_unsafe')
        try:
            return json.loads(path.read_text(encoding='utf-8'))
        except FileNotFoundError:
            return None

    def _write(self, name, value):
        if self.root is None:
            self._memory[name] = value
        else:
            write_record(self.root / name, value)

    def begin(self):
        """Reserve a fresh generation *before* sending its request."""
        with self.locked():
            request_id = uuid.uuid4().hex
            self._write('current.json', {**self.identity, 'request_id': request_id})
            return request_id

    def receive(self, request_id, raw):
        """Persist bytes before publishing receipt; late responses cannot win."""
        with self.locked():
            current = self._read('current.json')
            if current != {**self.identity, 'request_id': request_id}:
                raise ValueError('response_request_superseded')
            existing = self._read(request_id + '.json')
            if existing is not None:
                if existing.get('text') != raw:
                    raise ValueError('response_request_already_received')
                # An interrupted publication can be retried only with its exact
                # request and bytes, never by scanning orphan files.
                self._write('pending.json', existing['receipt'])
                return existing['receipt']
            receipt = {**current, 'raw_response_hash': text_hash(raw),
                'parser_version': PARSER_VERSION, 'validator_version': self.validator_version,
                'state': 'received'}
            # The immutable request record can survive a pointer-write failure;
            # pending() never scans it or infers that publication succeeded.
            self._write(request_id + '.json', {'receipt': receipt, 'text': raw})
            self._write('pending.json', receipt)
            return receipt

    def pending(self, *, include_failed=False):
        with self.locked():
            try:
                receipt = self._read('pending.json')
                if receipt is None:
                    return None
                current = self._read('current.json')
                if not isinstance(receipt, dict) or not isinstance(current, dict):
                    raise ValueError('response_identity_unproven')
                request_id = receipt['request_id']
                if (not isinstance(request_id, str) or not re.fullmatch('[0-9a-f]{32}', request_id)
                        or current != {**self.identity, 'request_id': request_id}
                        or any(receipt.get(k) != v for k, v in self.identity.items())):
                    raise ValueError('response_identity_unproven')
                saved = self._read(request_id + '.json')
                if (not isinstance(saved, dict) or not isinstance(saved.get('text'), str)
                        or saved.get('receipt') != receipt
                        or text_hash(saved['text']) != receipt['raw_response_hash']):
                    raise ValueError('response_identity_unproven')
                if not include_failed and receipt.get('failure_fingerprint') == self.failure_fingerprint(receipt['raw_response_hash']):
                    self.last_diagnostic = 'response_previous_failure'
                    return None
                self.last_diagnostic = None
                self.last_state = receipt['state']
                self.last_request_id = request_id
                return saved['text']
            except (ValueError, KeyError, TypeError):
                self.last_diagnostic = 'response_identity_unproven'
                return None

    def failure_fingerprint(self, raw_hash):
        return digest([raw_hash, PARSER_VERSION, self.validator_version, self.identity])

    def mark(self, raw, state, *, category=None):
        if state not in {'parse_failed', 'validation_failed', 'prepared'}:
            raise ValueError('response_state_invalid')
        with self.locked():
            receipt = self._read('pending.json')
            current = self._read('current.json')
            if (not receipt or current != {**self.identity, 'request_id': receipt.get('request_id')}
                    or receipt.get('raw_response_hash') != text_hash(raw)):
                raise ValueError('response_request_superseded')
            saved = self._read(receipt['request_id'] + '.json')
            if not saved or saved.get('text') != raw or saved.get('receipt') != receipt:
                raise ValueError('response_identity_unproven')
            updated = {**receipt, 'state': state, 'parser_version': PARSER_VERSION,
                       'validator_version': self.validator_version}
            try:
                updated['envelope'] = parse_model_json(raw).envelope
            except ModelJSONError:
                pass
            if category is not None:
                updated['category'] = category
            if state.endswith('_failed'):
                updated['failure_fingerprint'] = self.failure_fingerprint(text_hash(raw))
            else:
                updated.pop('failure_fingerprint', None)
            self._write(receipt['request_id'] + '.json', {'receipt': updated, 'text': raw})
            self._write('pending.json', updated)

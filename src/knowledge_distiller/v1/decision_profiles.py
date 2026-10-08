"""Private, explicitly rooted, atomic decision profiles; no settings integration.

Drafts are immutable. Validation and activation are separate explicit actions.
Secrets are resolved by reference and never serialized. The store is a private
configuration store, not an authenticity guarantee against its owning user.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import asdict
import json
import os
from pathlib import Path
import re
import stat
from threading import RLock
from typing import Callable
from uuid import uuid4

from .decision_client import (ChoiceAnswer, ChoiceQuestion, DecisionClient, DecisionError,
                              DecisionProfile, NoulAnswer, NoulQuestion)

_ID = re.compile(r'[a-f0-9]{32}')
_NAME = 'decision-profiles.json'
_LOCK = '.decision-profiles.lock'
_MAX_STORE_BYTES = 1024 * 1024
_UNSPECIFIED_ACTIVE = object()


class DecisionProfileError(RuntimeError):
    """Stable code only; persistence paths and contents are not echoed."""


def _pairs(items):
    result = {}
    for key, value in items:
        if key in result:
            raise ValueError('duplicate key')
        result[key] = value
    return result


class DecisionProfiles:
    def __init__(self, root: Path, *, secret: Callable[[str], str] = lambda ref: '', post=None, get=None):
        root = Path(root)
        if not root.is_absolute() or '..' in root.parts or root == Path(root.anchor):
            raise DecisionProfileError('decision_store_root_invalid')
        self.root = root
        self._secret, self._post, self._get = secret, post, get
        self._mutex = RLock()
        # Missing parents must be prepared explicitly by the caller. Reject all
        # symlink ancestors; use /private/tmp rather than macOS's /tmp alias.
        try:
            with self._directory(create=True):
                pass
        except OSError:
            raise DecisionProfileError('decision_store_unsafe') from None

    @contextmanager
    def _directory(self, *, create=False):
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        fd = os.open(self.root.anchor, flags)
        try:
            for index, part in enumerate(self.root.parts[1:]):
                if create and index == len(self.root.parts) - 2:
                    try:
                        os.mkdir(part, 0o700, dir_fd=fd)
                    except FileExistsError:
                        pass
                next_fd = os.open(part, flags, dir_fd=fd)
                os.close(fd)
                fd = next_fd
            metadata = os.fstat(fd)
            if metadata.st_uid != os.getuid() or stat.S_IMODE(metadata.st_mode) & 0o077:
                raise DecisionProfileError('decision_store_unsafe')
            yield fd
        finally:
            os.close(fd)

    @staticmethod
    def _safe_file(fd):
        metadata = os.fstat(fd)
        if (not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.getuid()
                or metadata.st_nlink != 1 or stat.S_IMODE(metadata.st_mode) & 0o077):
            raise DecisionProfileError('decision_store_unsafe')

    @contextmanager
    def _locked(self):
        import fcntl  # This batch targets macOS; no Windows persistence claim.
        with self._mutex:
            try:
                with self._directory() as directory:
                    lock = self._open_lock(directory)
                    try:
                        self._safe_file(lock)
                        fcntl.flock(lock, fcntl.LOCK_EX)
                        # A replacement while waiting must not split writers
                        # across different lock inodes or substitute a symlink.
                        self._safe_file(lock)
                        named = os.stat(_LOCK, dir_fd=directory, follow_symlinks=False)
                        opened = os.fstat(lock)
                        if (not stat.S_ISREG(named.st_mode)
                                or (named.st_dev, named.st_ino) != (opened.st_dev, opened.st_ino)):
                            raise DecisionProfileError('decision_store_unsafe')
                        yield directory
                    finally:
                        os.close(lock)
            except OSError:
                raise DecisionProfileError('decision_store_io_failed') from None

    @staticmethod
    def _open_lock(directory: int) -> int:
        # On this macOS host, simultaneous first openat(O_CREAT|O_NOFOLLOW)
        # returned ENOENT despite a valid directory and a newly visible lock.
        # Open existing first; create exclusively so a losing creator can only
        # reopen the winning regular file. Do not unlink/recreate a live lock.
        flags = os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK
        for _ in range(3):
            try:
                return os.open(_LOCK, flags, dir_fd=directory)
            except FileNotFoundError:
                try:
                    return os.open(_LOCK, flags | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=directory)
                except FileExistsError:
                    continue
        raise DecisionProfileError('decision_store_io_failed')

    def _read(self, directory: int) -> dict:
        try:
            fd = os.open(_NAME, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        except FileNotFoundError:
            return {'version': 2, 'active': None, 'profiles': {}, 'current_cloud_profile_id': None}
        try:
            self._safe_file(fd)
            with os.fdopen(fd, 'rb', closefd=False) as handle:
                encoded = handle.read(_MAX_STORE_BYTES + 1)
            if len(encoded) > _MAX_STORE_BYTES:
                raise DecisionProfileError('decision_store_invalid')
            state = json.loads(encoded, object_pairs_hook=_pairs,
                               parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)))
            self._validate(state)
            if state['version'] == 1:
                # In-memory projection only. Reads never rewrite the old file
                # or guess a cloud binding from inactive draft order.
                active = state['active']
                cloud = active if active is not None and state['profiles'][active]['profile']['provider'] == 'jev' else None
                state = {**state, 'version': 2, 'current_cloud_profile_id': cloud}
            return state
        except (ValueError, TypeError, KeyError, UnicodeError, DecisionError, RecursionError):
            raise DecisionProfileError('decision_store_invalid') from None
        finally:
            os.close(fd)

    @staticmethod
    def _validate(state: dict) -> None:
        if (not isinstance(state, dict) or type(state.get('version')) is not int
                or state['version'] not in (1, 2)
                or set(state) != ({'version', 'active', 'profiles'} if state['version'] == 1 else
                                  {'version', 'active', 'profiles', 'current_cloud_profile_id'})
                or not isinstance(state['profiles'], dict)):
            raise ValueError('shape')
        for identity, row in state['profiles'].items():
            if (not isinstance(identity, str) or not _ID.fullmatch(identity) or not isinstance(row, dict)
                    or set(row) != {'profile', 'validation'} or not isinstance(row['profile'], dict)
                    or set(row['profile']) != set(DecisionProfile.__dataclass_fields__)):
                raise ValueError('profile')
            profile = DecisionProfile(**row['profile'])
            validation = row['validation']
            if validation is not None:
                if (not isinstance(validation, dict) or set(validation) != {'contract', 'model', 'provider', 'served_model'}
                        or validation['contract'] != 'synthetic-choice-noul-v1'
                        or validation['provider'] != profile.provider
                        or not isinstance(validation['model'], str) or not validation['model'].strip()
                        or (profile.provider == 'clef' and (validation['model'] != profile.model
                            or validation['served_model'] != profile.model))
                        or (profile.provider == 'jev' and validation['served_model'] is not None)):
                    raise ValueError('validation')
        active = state['active']
        if active is not None and (not isinstance(active, str) or active not in state['profiles']
                                   or state['profiles'][active]['validation'] is None):
            raise ValueError('active')
        if state['version'] == 2:
            cloud = state['current_cloud_profile_id']
            if cloud is not None and (not isinstance(cloud, str) or cloud not in state['profiles']
                    or state['profiles'][cloud]['profile']['provider'] != 'jev'
                    or state['profiles'][cloud]['validation'] is None):
                raise ValueError('cloud')
            if active is not None and state['profiles'][active]['profile']['provider'] == 'jev' and cloud != active:
                raise ValueError('active_cloud')

    def _write(self, directory: int, state: dict) -> None:
        self._validate(state)
        body = json.dumps(state, ensure_ascii=False, sort_keys=True, allow_nan=False).encode('utf-8')
        if len(body) > _MAX_STORE_BYTES:
            raise DecisionProfileError('decision_store_full')
        temp = '.decision-profiles-' + uuid4().hex + '.tmp'
        fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                     0o600, dir_fd=directory)
        try:
            with os.fdopen(fd, 'wb', closefd=False) as handle:
                handle.write(body)
                handle.flush()
                os.fsync(fd)
            # All operations use the locked directory descriptor; existing
            # target was safely read before reaching this replacement.
            os.replace(temp, _NAME, src_dir_fd=directory, dst_dir_fd=directory)
            os.fsync(directory)
        finally:
            os.close(fd)
            try:
                os.unlink(temp, dir_fd=directory)
            except FileNotFoundError:
                pass

    @staticmethod
    def _row(state, identity):
        if not isinstance(identity, str) or identity not in state['profiles']:
            raise DecisionProfileError('decision_profile_missing')
        return state['profiles'][identity]

    def add_draft(self, profile: DecisionProfile) -> str:
        if not isinstance(profile, DecisionProfile):
            raise DecisionProfileError('decision_profile_invalid')
        with self._locked() as directory:
            state = self._read(directory)
            identity = uuid4().hex
            state['profiles'][identity] = {'profile': asdict(profile), 'validation': None}
            self._write(directory, state)
            return identity

    def get(self, identity: str) -> DecisionProfile:
        with self._locked() as directory:
            row = self._row(self._read(directory), identity)
            return DecisionProfile(**row['profile'])

    def active(self) -> tuple[str, DecisionProfile] | None:
        with self._locked() as directory:
            state = self._read(directory)
            identity = state['active']
            if identity is None:
                return None
            return identity, DecisionProfile(**state['profiles'][identity]['profile'])

    def client(self, identity: str) -> DecisionClient:
        return DecisionClient(self.get(identity), secret=self._secret, post=self._post, get=self._get)

    def bindings(self) -> dict:
        """Read both references from one locked snapshot; never load secrets."""
        with self._locked() as directory:
            state = self._read(directory)
            def descriptor(identity):
                return None if identity is None else (identity, DecisionProfile(**state['profiles'][identity]['profile']))
            return {'active': descriptor(state['active']),
                    'current_cloud': descriptor(state['current_cloud_profile_id'])}

    def qualification(self, identity: str) -> dict:
        """Read this immutable draft's eligibility without checking any service."""
        with self._locked() as directory:
            state = self._read(directory)
            row = self._row(state, identity)
            return {'checked': row['validation'] is not None, 'active': state['active'] == identity}

    def validate_draft(self, identity: str):
        """Explicit served-name check plus synthetic typed inference. Never activates.

        Failed revalidation clears eligibility for this draft but preserves the
        old active profile. Active rows are immutable; test a new draft instead.
        """
        with self._locked() as directory:
            state = self._read(directory)
            row = self._row(state, identity)
            if identity in (state['active'], state['current_cloud_profile_id']):
                raise DecisionProfileError('decision_active_immutable')
            row['validation'] = None
            self._write(directory, state)
            profile = DecisionProfile(**row['profile'])
            # Serialize validation and activation across service instances and
            # processes. A failed concurrent probe cannot leave stale eligibility.
            client = DecisionClient(profile, secret=self._secret, post=self._post, get=self._get)
            served_model = client.verify_local_model() if profile.provider == 'clef' else None
            result = client.ask(
                {'marker': 'r17-synthetic-connection-check'},
                {'choice': ChoiceQuestion('Choose the marker in `state.marker`.',
                                          {'match': 'r17-synthetic-connection-check', 'other': 'different marker'}),
                 'noul': NoulQuestion('Is `state.marker` exactly r17-synthetic-connection-check?')})
            choice, noul = result.answers['choice'], result.answers['noul']
            if (not isinstance(choice, ChoiceAnswer) or choice.choice != 'match'
                    or not isinstance(noul, NoulAnswer) or noul.noul <= 0.5):
                raise DecisionError('decision_probe_failed')
            row['validation'] = {'contract': 'synthetic-choice-noul-v1',
                                 'provider': result.provider, 'model': result.model, 'served_model': served_model}
            self._write(directory, state)
        return result

    def activate(self, identity: str, *, expected_active_id=_UNSPECIFIED_ACTIVE,
                 expected_current_cloud_profile_id=_UNSPECIFIED_ACTIVE) -> None:
        if expected_current_cloud_profile_id is not _UNSPECIFIED_ACTIVE and expected_active_id is _UNSPECIFIED_ACTIVE:
            raise DecisionProfileError('decision_active_conflict')
        with self._locked() as directory:
            state = self._read(directory)
            if expected_active_id is not _UNSPECIFIED_ACTIVE and state['active'] != expected_active_id:
                raise DecisionProfileError('decision_active_conflict')
            if (expected_current_cloud_profile_id is not _UNSPECIFIED_ACTIVE
                    and state['current_cloud_profile_id'] != expected_current_cloud_profile_id):
                raise DecisionProfileError('decision_cloud_conflict')
            row = self._row(state, identity)
            if row['validation'] is None:
                raise DecisionProfileError('decision_profile_unvalidated')
            state['active'] = identity
            if row['profile']['provider'] == 'jev':
                state['current_cloud_profile_id'] = identity
            self._write(directory, state)

    def activate_bound(self, identity: str, *, expected_active_id,
                       expected_current_cloud_profile_id) -> None:
        """New activation requires both CAS bases; old activate stays compatible."""
        self.activate(identity, expected_active_id=expected_active_id,
                      expected_current_cloud_profile_id=expected_current_cloud_profile_id)

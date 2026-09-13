"""Content-addressed, offline document models shared by installers and runtime.

The trusted inventory ships with the program. An imported manifest is never an
authority for file identity. Activation is the rename of a verified private copy;
neither the old application nor an existing model version is edited in place.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import stat
import tempfile
import zipfile
from uuid import uuid4


class DoclingComponentError(RuntimeError):
    pass


def trusted_manifest():
    return json.loads((Path(__file__).parent / 'adapters' /
                       'docling-models-manifest.json').read_text(encoding='utf-8'))


from .windows_platform import filesystem_path


def _ordinary(path, *, directory=False):
    info = path.lstat()
    if (stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400
            or not (stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode))):
        raise DoclingComponentError('docling_component_unsafe_path')


class DoclingComponent:
    def __init__(self, components_root, *, manifest=None):
        self.manifest = trusted_manifest() if manifest is None else manifest
        self.files = self.manifest['files']
        # Git checkout newline conversion must not change shared archive identity.
        self.notices = (Path(__file__).parent / 'adapters' / 'docling-model-notices.txt').read_bytes().replace(b'\r\n', b'\n')
        if not self.files:
            raise DoclingComponentError('docling_component_invalid_inventory')
        seen = set()
        for name, entry in self.files.items():
            path = PurePosixPath(name)
            if (not name or path.is_absolute() or '..' in path.parts or '\\' in name
                    or ':' in name or str(path) != name or name.casefold() in seen
                    or type(entry['size']) is not int or entry['size'] < 0
                    or len(entry['sha256']) != 64
                    or any(c not in '0123456789abcdef' for c in entry['sha256'])):
                raise DoclingComponentError('docling_component_invalid_inventory')
            seen.add(name.casefold())
        canonical = json.dumps(self.manifest, sort_keys=True, separators=(',', ':')).encode()
        self.identity = hashlib.sha256(canonical).hexdigest()
        self.root = filesystem_path(Path(components_root) / 'docling')
        self.active = self.root / self.identity

    def verify(self, root=None):
        root = self.active if root is None else filesystem_path(root)
        try:
            _ordinary(root, directory=True)
            for name, entry in self.files.items():
                path = root / name
                for parent in path.relative_to(root).parents:
                    _ordinary(root / parent, directory=True)
                _ordinary(path)
                with path.open('rb') as stream:
                    if (os.fstat(stream.fileno()).st_size != entry['size'] or
                            hashlib.file_digest(stream, 'sha256').hexdigest() != entry['sha256']):
                        raise DoclingComponentError('docling_component_corrupt')
        except FileNotFoundError as error:
            raise DoclingComponentError('docling_component_missing') from error
        return root

    def import_existing(self, source):
        """Copy only known bytes; a crash leaves at most an unactivated staging dir."""
        source = self.verify(source)
        self.root.mkdir(parents=True, exist_ok=True)
        _ordinary(self.root, directory=True)
        if self.active.exists():
            try:
                return self.verify()
            except DoclingComponentError:
                pass
        staging = Path(tempfile.mkdtemp(prefix='.import-', dir=self.root))
        try:
            for name in self.files:
                destination = staging / name
                destination.parent.mkdir(parents=True, exist_ok=True)
                with (source / name).open('rb') as src, destination.open('xb') as dst:
                    shutil.copyfileobj(src, dst, 1024 * 1024)
                    dst.flush()
                    os.fsync(dst.fileno())
            (staging / 'manifest.json').write_text(
                json.dumps(self.manifest, ensure_ascii=False, indent=2), encoding='utf-8')
            (staging / 'NOTICE.txt').write_bytes(self.notices)
            self.verify(staging)
            self._activate(staging)
            return self.verify()
        finally:
            if staging.exists():
                shutil.rmtree(staging)

    def import_archive(self, archive):
        """Unpack exactly the trusted inventory with bounded streaming writes.

        The release downloader also checks its signed archive identity. This
        independent content check applies even to an offline installer asset.
        """
        self.root.mkdir(parents=True, exist_ok=True)
        _ordinary(self.root, directory=True)
        if self.active.exists():
            try:
                return self.verify()
            except DoclingComponentError:
                pass
        staging = Path(tempfile.mkdtemp(prefix='.import-', dir=self.root))
        try:
            with zipfile.ZipFile(filesystem_path(archive)) as package:
                entries = package.infolist()
                expected_files = dict(self.files)
                if 'NOTICE.txt' in package.namelist():
                    expected_files['NOTICE.txt'] = {'size': len(self.notices),
                        'sha256': hashlib.sha256(self.notices).hexdigest()}
                if (len(entries) != len(expected_files) or
                        {entry.filename for entry in entries} != set(expected_files)):
                    raise DoclingComponentError('docling_component_invalid_archive')
                for entry in entries:
                    expected = expected_files[entry.filename]
                    mode = entry.external_attr >> 16
                    if (entry.file_size != expected['size'] or entry.flag_bits & 1 or
                            stat.S_ISLNK(mode) or entry.is_dir()):
                        raise DoclingComponentError('docling_component_invalid_archive')
                    path = staging / entry.filename
                    path.parent.mkdir(parents=True, exist_ok=True)
                    count, digest = 0, hashlib.sha256()
                    with package.open(entry) as source, path.open('xb') as target:
                        while block := source.read(min(1024 * 1024, expected['size'] - count + 1)):
                            count += len(block)
                            if count > expected['size']:
                                raise DoclingComponentError('docling_component_corrupt')
                            digest.update(block)
                            target.write(block)
                        target.flush()
                        os.fsync(target.fileno())
                    if count != expected['size'] or digest.hexdigest() != expected['sha256']:
                        raise DoclingComponentError('docling_component_corrupt')
            (staging / 'manifest.json').write_text(json.dumps(self.manifest), encoding='utf-8')
            (staging / 'NOTICE.txt').write_bytes(self.notices)
            self.verify(staging)
            self._activate(staging)
            return self.verify()
        finally:
            if staging.exists():
                shutil.rmtree(staging)

    def _activate(self, staging):
        from .file_lock import acquire
        with acquire(self.root / '.activation.lock'):
            retained = None
            if self.active.exists():
                try:
                    self.verify()
                    return  # A concurrent installer activated the same bytes.
                except DoclingComponentError:
                    retained = self.root / ('.retained-corrupt-' + uuid4().hex)
                    self.active.rename(retained)
            try:
                staging.rename(self.active)
            except BaseException:
                if retained is not None and not self.active.exists():
                    retained.rename(self.active)
                raise

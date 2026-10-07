"""Offline schema-upgrade proof for an explicitly marked disposable data copy."""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import sqlite3
import stat
import struct
import uuid
from contextlib import closing
from pathlib import Path

from .database import SCHEMA_VERSION, initialize


MARKER_NAME = ".kd-offline-upgrade.json"
MARKER_PURPOSE = "knowledge-distiller-v3-offline-upgrade"
DATABASE_NAME = "knowledge.sqlite3"
VAULT_RELPATH = "synthetic-vault"
_SHA256_LENGTH = 64


class UpgradeProbeError(RuntimeError):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def _absolute(path: Path | str) -> Path:
    raw = Path(path).expanduser()
    if not raw.is_absolute() or ".." in raw.parts:
        raise UpgradeProbeError("fixture_invalid")
    return Path(os.path.normpath(str(raw)))


def _reject_symlinks(path: Path, *, missing_leaf: bool = False) -> Path:
    path = _absolute(path)
    current = Path(path.anchor)
    for index, part in enumerate(path.parts[1:], start=1):
        current /= part
        try:
            info = current.lstat()
        except FileNotFoundError:
            if missing_leaf and index == len(path.parts) - 1:
                return path
            raise UpgradeProbeError("fixture_invalid") from None
        if stat.S_ISLNK(info.st_mode):
            raise UpgradeProbeError("fixture_invalid")
    return path


def _mode(path: Path, expected: int, *, directory: bool) -> os.stat_result:
    path = _reject_symlinks(path)
    info = path.lstat()
    correct_type = stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)
    if not correct_type or stat.S_IMODE(info.st_mode) != expected:
        raise UpgradeProbeError("fixture_invalid")
    return info


def _related(left: Path, right: Path) -> bool:
    return left == right or left in right.parents or right in left.parents


def _digest_file(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _digest_tree(root: Path) -> str:
    digest = hashlib.sha256()
    digest.update(b"kd-synthetic-vault-v1\0")
    for path in sorted(root.rglob("*"), key=lambda item: item.relative_to(root).as_posix()):
        info = path.lstat()
        if stat.S_ISLNK(info.st_mode):
            raise UpgradeProbeError("fixture_invalid")
        relative = path.relative_to(root).as_posix().encode("utf-8")
        if stat.S_ISDIR(info.st_mode):
            kind = b"D"
        elif stat.S_ISREG(info.st_mode):
            kind = b"F"
        else:
            raise UpgradeProbeError("fixture_invalid")
        digest.update(kind + len(relative).to_bytes(8, "big") + relative)
        digest.update(stat.S_IMODE(info.st_mode).to_bytes(4, "big"))
        if kind == b"F":
            digest.update(info.st_size.to_bytes(8, "big"))
            with path.open("rb") as stream:
                while chunk := stream.read(1024 * 1024):
                    digest.update(chunk)
    return digest.hexdigest()


def _read_marker(path: Path) -> dict:
    _mode(path, 0o600, directory=False)
    if path.stat().st_size > 16 * 1024:
        raise UpgradeProbeError("fixture_invalid")
    try:
        marker = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        raise UpgradeProbeError("fixture_invalid") from None
    expected = {
        "schema_version", "purpose", "database_name", "database_before_sha256",
        "expected_schema", "vault_relpath", "vault_tree_sha256",
    }
    if (not isinstance(marker, dict) or set(marker) != expected
            or marker.get("schema_version") != 1
            or marker.get("purpose") != MARKER_PURPOSE
            or marker.get("database_name") != DATABASE_NAME
            or marker.get("expected_schema") != 21
            or marker.get("vault_relpath") != VAULT_RELPATH):
        raise UpgradeProbeError("fixture_invalid")
    for key in ("database_before_sha256", "vault_tree_sha256"):
        value = marker.get(key)
        if (not isinstance(value, str) or len(value) != _SHA256_LENGTH
                or any(character not in "0123456789abcdef" for character in value)):
            raise UpgradeProbeError("fixture_invalid")
    return marker


def _sqlite_readonly(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
    try:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only = ON")
    except Exception:
        connection.close()
        raise
    return connection


def _hash_value(digest: "hashlib._Hash", value) -> None:
    if value is None:
        tag, payload = b"N", b""
    elif type(value) is int:
        tag, payload = b"I", str(value).encode("ascii")
    elif type(value) is float:
        tag, payload = b"F", struct.pack(">d", value)
    elif type(value) is str:
        tag, payload = b"T", value.encode("utf-8")
    elif isinstance(value, bytes):
        tag, payload = b"B", value
    else:
        raise UpgradeProbeError("precheck_failed")
    digest.update(tag + len(payload).to_bytes(8, "big") + payload)


def _quote_identifier(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def _legacy_identity(connection: sqlite3.Connection) -> tuple[dict[str, int], str]:
    objects = list(connection.execute(
        """SELECT type,name,sql FROM sqlite_master
           WHERE name NOT LIKE 'sqlite_%' AND name NOT LIKE 'wiki_%'
           ORDER BY type,name"""))
    digest = hashlib.sha256()
    digest.update(b"kd-legacy-schema-and-rows-v1\0")
    for row in objects:
        for value in row:
            _hash_value(digest, value)
    tables = [(row[1], row[2] or "") for row in objects if row[0] == "table"]
    counts: dict[str, int] = {}
    for name, sql in tables:
        quoted = _quote_identifier(name)
        columns = list(connection.execute(f"PRAGMA table_info({quoted})"))
        if not columns:
            raise UpgradeProbeError("precheck_failed")
        counts[name] = int(connection.execute(f"SELECT COUNT(*) FROM {quoted}").fetchone()[0])
        if "WITHOUT ROWID" in sql.upper():
            primary = [row[1] for row in sorted(columns, key=lambda row: row[5]) if row[5]]
            order = ",".join(_quote_identifier(column) for column in primary)
        else:
            order = "rowid"
        query = f"SELECT * FROM {quoted}" + (f" ORDER BY {order}" if order else "")
        _hash_value(digest, name)
        for row in connection.execute(query):
            digest.update(len(row).to_bytes(4, "big"))
            for value in row:
                _hash_value(digest, value)
    return counts, digest.hexdigest()


def _snapshot(database: Path, expected_vault: Path) -> dict:
    with closing(_sqlite_readonly(database)) as connection:
        schema = int(connection.execute("PRAGMA user_version").fetchone()[0])
        quick_rows = [tuple(row) for row in connection.execute("PRAGMA quick_check")]
        foreign_key_violations = sum(1 for _ in connection.execute("PRAGMA foreign_key_check"))
        vault_rows = list(connection.execute(
            "SELECT value FROM settings WHERE key = 'vault_path'"))
        if len(vault_rows) != 1 or vault_rows[0][0] != str(expected_vault):
            raise UpgradeProbeError("fixture_invalid")
        counts, legacy_digest = _legacy_identity(connection)
    return {
        "schema": schema,
        "quick_check": quick_rows == [("ok",)],
        "foreign_key_violations": foreign_key_violations,
        "legacy_table_counts": counts,
        "legacy_digest": legacy_digest,
    }


def _report_target(path: Path, formal_root: Path) -> Path:
    path = _absolute(path)
    if path.exists() or path.is_symlink():
        raise UpgradeProbeError("report_exists")
    parent = _reject_symlinks(path.parent)
    _mode(parent, 0o700, directory=True)
    if _related(parent, formal_root):
        raise UpgradeProbeError("fixture_invalid")
    return path


def _write_report(path: Path, value: dict) -> None:
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    descriptor = None
    try:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            descriptor = None
            json.dump(value, stream, ensure_ascii=False, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if descriptor is not None:
            os.close(descriptor)
        temporary.unlink(missing_ok=True)


def _failure(code: str) -> dict:
    return {"schema_version": 1, "ok": False, "error_code": code}


def _try_write_report(path: Path, value: dict) -> bool:
    try:
        _write_report(path, value)
    except Exception:
        return False
    return True


def run(data_root: Path | str, report: Path | str, *, formal_root: Path | str) -> int:
    try:
        formal = _absolute(formal_root)
        report_path = _report_target(Path(report), formal)
    except Exception:
        return 1
    try:
        root = _reject_symlinks(Path(data_root))
        _mode(root, 0o700, directory=True)
        if _related(root, formal):
            raise UpgradeProbeError("fixture_invalid")
        if root == report_path.parent or root in report_path.parents:
            return 1
        marker = _read_marker(root / MARKER_NAME)
        database = root / DATABASE_NAME
        database_stat = _mode(database, 0o600, directory=False)
        if database_stat.st_nlink != 1:
            raise UpgradeProbeError("fixture_invalid")
        if (database.with_name(database.name + "-wal").exists()
                or database.with_name(database.name + "-shm").exists()):
            raise UpgradeProbeError("fixture_invalid")
        vault = root / VAULT_RELPATH
        _mode(vault, 0o700, directory=True)
        vault_before = _digest_tree(vault)
        if vault_before != marker["vault_tree_sha256"]:
            raise UpgradeProbeError("fixture_invalid")
        database_before = _digest_file(database)
        if database_before != marker["database_before_sha256"]:
            raise UpgradeProbeError("database_changed")
        lock_path = root / ".instance.lock"
        try:
            lock_descriptor = os.open(
                lock_path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        except OSError:
            raise UpgradeProbeError("fixture_invalid") from None
        try:
            if not stat.S_ISREG(os.fstat(lock_descriptor).st_mode):
                raise UpgradeProbeError("fixture_invalid")
            try:
                fcntl.flock(lock_descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise UpgradeProbeError("fixture_busy") from None
            current_stat = database.lstat()
            if ((current_stat.st_dev, current_stat.st_ino)
                    != (database_stat.st_dev, database_stat.st_ino)
                    or _digest_file(database) != database_before):
                raise UpgradeProbeError("database_changed")
            try:
                before = _snapshot(database, vault)
            except UpgradeProbeError:
                raise
            except (OSError, sqlite3.Error):
                raise UpgradeProbeError("precheck_failed") from None
            if (before["schema"] != marker["expected_schema"]
                    or not before["quick_check"]
                    or before["foreign_key_violations"]):
                raise UpgradeProbeError(
                    "unsupported_schema" if before["schema"] != marker["expected_schema"]
                    else "precheck_failed")
            try:
                initialize(database)
            except Exception:
                raise UpgradeProbeError("migration_failed") from None
            current_stat = database.lstat()
            if (current_stat.st_dev, current_stat.st_ino) != (database_stat.st_dev, database_stat.st_ino):
                raise UpgradeProbeError("postcheck_failed")
            try:
                after = _snapshot(database, vault)
            except (OSError, sqlite3.Error, UpgradeProbeError):
                raise UpgradeProbeError("postcheck_failed") from None
            vault_after = _digest_tree(vault)
            if (after["schema"] != SCHEMA_VERSION or not after["quick_check"]
                    or after["foreign_key_violations"]):
                raise UpgradeProbeError("postcheck_failed")
            unchanged = (before["legacy_table_counts"] == after["legacy_table_counts"]
                         and before["legacy_digest"] == after["legacy_digest"])
            if not unchanged or vault_after != vault_before:
                raise UpgradeProbeError("legacy_changed")
            result = {
                "schema_version": 1,
                "ok": True,
                "error_code": None,
                "database": {
                    "schema_before": before["schema"],
                    "schema_after": after["schema"],
                    "quick_check_before": before["quick_check"],
                    "quick_check_after": after["quick_check"],
                    "foreign_key_violations_before": before["foreign_key_violations"],
                    "foreign_key_violations_after": after["foreign_key_violations"],
                    "sha256_before": database_before,
                    "sha256_after": _digest_file(database),
                },
                "legacy": {
                    "table_counts_before": before["legacy_table_counts"],
                    "table_counts_after": after["legacy_table_counts"],
                    "digest_before": before["legacy_digest"],
                    "digest_after": after["legacy_digest"],
                    "unchanged": True,
                },
                "vault": {
                    "sha256_before": vault_before,
                    "sha256_after": vault_after,
                    "unchanged": True,
                },
            }
        finally:
            os.close(lock_descriptor)
    except UpgradeProbeError as error:
        _try_write_report(report_path, _failure(error.code))
        return 1
    except Exception:
        _try_write_report(report_path, _failure("internal_error"))
        return 1
    return 0 if _try_write_report(report_path, result) else 1

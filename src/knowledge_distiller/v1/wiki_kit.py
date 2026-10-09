"""Verify and plan upgrades for the independently installed Vault kit."""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat

from .wiki_lock import WikiLockError, canonical_vault


RECEIPT_PATH = PurePosixPath(".kd/wiki-kit.json")
_VERSION = re.compile(r"[A-Za-z0-9._-]{1,40}\Z")
_HASH = re.compile(r"[0-9a-f]{64}\Z")


class WikiKitError(RuntimeError):
    """A fixed-code kit verification failure."""


@dataclass(frozen=True)
class KitFile:
    source_path: str
    install_path: str
    sha256: str


@dataclass(frozen=True)
class KitManifest:
    kit_version: str
    protocol_version: int
    manifest_sha256: str
    files: tuple[KitFile, ...]


@dataclass(frozen=True)
class UpgradeAction:
    source_path: str
    install_path: str
    sha256: str
    expected_before: str | None


# V2.0 was distributed without a receipt.  These are the exact seven Git blobs
# declared for manual installation by tag v2026.09.30.4 at
# ea0896a42b19e9b3fd464e78492a7bd535561869.  This closed identity is the only
# unreceipted installation that V3 may adopt.
_V2_UNRECEIPTED_FILES = (
    KitFile("AGENTS.md", "AGENTS.md",
            "11cde287539983c48d3506a38336b4323685550463d9e37733150e8c548b2cc6"),
    KitFile("CLAUDE.md", "CLAUDE.md",
            "373b06b72e1ccc4851755bc6ebd2b7e45298397289363d4041b3610c15a2e423"),
    KitFile("tools/kb.py", "tools/kb.py",
            "95843adb3fe7ec4aa2421a5c77b2deafa4606c64950002975260147d1367805e"),
    KitFile("agent-skills/kb-confirm/SKILL.md", ".agents/skills/kb-confirm/SKILL.md",
            "64645bb356c4b91066b649cb7da138c0ebb84b030053f0c56f1d62c222e8418c"),
    KitFile("agent-skills/kb-ingest/SKILL.md", ".agents/skills/kb-ingest/SKILL.md",
            "fdab61a1add73b03c03174dc737f75f9930d4c8fdffdf17e3c06ea0e426e7e78"),
    KitFile("agent-skills/kb-lint/SKILL.md", ".agents/skills/kb-lint/SKILL.md",
            "140298f393959092983c49ea3926761beec0a8e5134eab92910e97eb8c2294b5"),
    KitFile("agent-skills/kb-read/SKILL.md", ".agents/skills/kb-read/SKILL.md",
            "b1792923b8656c59ba7c32ba05430ed78fb7c04733893e826b044029896ac2f8"),
)


def _safe_root(value: Path | str) -> Path:
    try:
        return canonical_vault(value)
    except WikiLockError as error:
        if str(error) == "vault_symlink":
            raise WikiKitError("kit_symlink") from error
        raise WikiKitError("kit_read_failed") from error


def _relative(value: object, *, install: bool) -> str:
    if not isinstance(value, str):
        raise WikiKitError("kit_manifest_invalid")
    path = PurePosixPath(value)
    if path.is_absolute() or not path.parts or any(part in ("", ".", "..") for part in path.parts):
        raise WikiKitError("kit_manifest_invalid")
    if install and not install_path_allowed(path.as_posix()):
        raise WikiKitError("kit_manifest_invalid")
    return path.as_posix()


def install_path_allowed(value: str) -> bool:
    """Limit kit ownership to the documented protocol and tool locations."""
    path = PurePosixPath(value)
    if path.as_posix() in {"AGENTS.md", "CLAUDE.md"}:
        return True
    parts = path.parts
    return (len(parts) >= 2 and parts[0] == "tools"
            or len(parts) >= 4 and parts[:2] == (".agents", "skills")
            or len(parts) >= 3 and parts[:2] == (".kd", "assets"))


def _regular_bytes(root: Path, relative: str, *, missing_code: str) -> bytes:
    current = root
    try:
        for part in PurePosixPath(relative).parts:
            current /= part
            info = current.lstat()
            if stat.S_ISLNK(info.st_mode):
                raise WikiKitError("kit_symlink")
        if not stat.S_ISREG(current.lstat().st_mode):
            raise WikiKitError("kit_invalid_file")
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(current, flags)
        try:
            before = os.fstat(descriptor)
            if not stat.S_ISREG(before.st_mode):
                raise WikiKitError("kit_invalid_file")
            chunks = []
            while True:
                chunk = os.read(descriptor, 1024 * 1024)
                if not chunk:
                    break
                chunks.append(chunk)
            after = os.fstat(descriptor)
            if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (
                    after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns):
                raise WikiKitError("kit_read_changed")
            return b"".join(chunks)
        finally:
            os.close(descriptor)
    except WikiKitError:
        raise
    except FileNotFoundError as error:
        raise WikiKitError(missing_code) from error
    except OSError as error:
        raise WikiKitError("kit_read_failed") from error


def _decode_manifest(raw: bytes, digest: str) -> KitManifest:
    try:
        data = json.loads(raw)
    except (UnicodeError, json.JSONDecodeError) as error:
        raise WikiKitError("kit_manifest_invalid") from error
    if not isinstance(data, dict) or set(data) != {"kit_version", "protocol_version", "files"}:
        raise WikiKitError("kit_manifest_invalid")
    version = data["kit_version"]
    protocol = data["protocol_version"]
    rows = data["files"]
    if not isinstance(version, str) or not _VERSION.fullmatch(version):
        raise WikiKitError("kit_manifest_invalid")
    if not isinstance(protocol, int) or isinstance(protocol, bool) or protocol < 1:
        raise WikiKitError("kit_manifest_invalid")
    if not isinstance(rows, list) or not rows:
        raise WikiKitError("kit_manifest_invalid")
    files = []
    source_seen, install_seen = set(), set()
    for row in rows:
        if not isinstance(row, dict) or set(row) != {"source_path", "install_path", "sha256"}:
            raise WikiKitError("kit_manifest_invalid")
        source = _relative(row["source_path"], install=False)
        target = _relative(row["install_path"], install=True)
        sha256 = row["sha256"]
        if not isinstance(sha256, str) or not _HASH.fullmatch(sha256):
            raise WikiKitError("kit_manifest_invalid")
        if source in source_seen or target in install_seen:
            raise WikiKitError("kit_manifest_invalid")
        source_seen.add(source)
        install_seen.add(target)
        files.append(KitFile(source, target, sha256))
    return KitManifest(version, protocol, digest, tuple(files))


def verify_source_kit(kit_root: Path | str) -> KitManifest:
    root = _safe_root(kit_root)
    raw = _regular_bytes(root, "kit-manifest.json", missing_code="kit_missing")
    manifest = _decode_manifest(raw, hashlib.sha256(raw).hexdigest())
    for item in manifest.files:
        content = _regular_bytes(root, item.source_path, missing_code="kit_missing")
        if hashlib.sha256(content).hexdigest() != item.sha256:
            raise WikiKitError("kit_drift")
    return manifest


def source_file_bytes(kit_root: Path | str, item: KitFile) -> bytes:
    """Read one manifest source through the same no-symlink stable-file boundary."""
    root = _safe_root(kit_root)
    content = _regular_bytes(root, item.source_path, missing_code="kit_missing")
    if hashlib.sha256(content).hexdigest() != item.sha256:
        raise WikiKitError("kit_drift")
    return content


def receipt_bytes(manifest: KitManifest) -> bytes:
    """Return the canonical private ownership receipt for an installed manifest."""
    payload = {
        "kit_version": manifest.kit_version,
        "protocol_version": manifest.protocol_version,
        "manifest_sha256": manifest.manifest_sha256,
        "files": [item.__dict__ for item in manifest.files],
    }
    return (json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8")


def _receipt_file(vault: Path) -> Path:
    return vault.joinpath(*RECEIPT_PATH.parts)


def read_receipt(vault: Path | str) -> KitManifest | None:
    root = _safe_root(vault)
    target = _receipt_file(root)
    if not target.exists() and not target.is_symlink():
        return None
    raw = _regular_bytes(root, RECEIPT_PATH.as_posix(), missing_code="kit_missing")
    try:
        data = json.loads(raw)
    except (UnicodeError, json.JSONDecodeError) as error:
        raise WikiKitError("kit_receipt_invalid") from error
    if not isinstance(data, dict) or set(data) != {
        "kit_version", "protocol_version", "manifest_sha256", "files"
    }:
        raise WikiKitError("kit_receipt_invalid")
    manifest_hash = data.pop("manifest_sha256")
    if not isinstance(manifest_hash, str) or not _HASH.fullmatch(manifest_hash):
        raise WikiKitError("kit_receipt_invalid")
    normalized = json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    try:
        receipt = _decode_manifest(normalized, manifest_hash)
    except WikiKitError as error:
        raise WikiKitError("kit_receipt_invalid") from error
    return receipt


def verify_installed_kit(vault: Path | str, receipt: KitManifest) -> None:
    root = _safe_root(vault)
    for item in receipt.files:
        content = _regular_bytes(root, item.install_path, missing_code="kit_drift")
        if hashlib.sha256(content).hexdigest() != item.sha256:
            raise WikiKitError("kit_drift")


def _target_exists_without_symlink(root: Path, relative: str) -> bool:
    current = root
    for part in PurePosixPath(relative).parts:
        current /= part
        try:
            info = current.lstat()
        except FileNotFoundError:
            return False
        except OSError as error:
            raise WikiKitError("kit_read_failed") from error
        if stat.S_ISLNK(info.st_mode):
            raise WikiKitError("kit_symlink")
    return True


def _unreceipted_v2_baseline(root: Path, desired: KitManifest) -> dict[str, str] | None:
    """Return exact V2 ownership, reject every partial or ambiguous lookalike."""
    legacy = {item.install_path: item.sha256 for item in _V2_UNRECEIPTED_FILES}
    desired_paths = {item.install_path for item in desired.files}
    presence = {
        relative: _target_exists_without_symlink(root, relative)
        for relative in legacy
    }
    if not any(presence.values()):
        return None
    if not legacy.keys() <= desired_paths or not all(presence.values()):
        raise WikiKitError("kit_unmanaged_target")
    for relative, expected in legacy.items():
        content = _regular_bytes(root, relative, missing_code="kit_unmanaged_target")
        if hashlib.sha256(content).hexdigest() != expected:
            raise WikiKitError("kit_unmanaged_target")
    for relative in desired_paths - legacy.keys():
        if _target_exists_without_symlink(root, relative):
            raise WikiKitError("kit_unmanaged_target")
    return legacy


def plan_upgrade(vault: Path | str, kit_root: Path | str) -> tuple[KitManifest, tuple[UpgradeAction, ...]]:
    """Verify current ownership and return a write plan without changing files."""
    root = _safe_root(vault)
    desired = verify_source_kit(kit_root)
    receipt = read_receipt(root)
    if receipt is None:
        legacy = _unreceipted_v2_baseline(root, desired)
        if legacy is not None:
            return desired, tuple(
                UpgradeAction(item.source_path, item.install_path, item.sha256,
                              legacy.get(item.install_path))
                for item in desired.files
                if legacy.get(item.install_path) != item.sha256
            )
        if any(_target_exists_without_symlink(root, item.install_path) for item in desired.files):
            raise WikiKitError("kit_unmanaged_target")
        return desired, tuple(UpgradeAction(x.source_path, x.install_path, x.sha256, None)
                              for x in desired.files)

    verify_installed_kit(root, receipt)
    old_by_target = {item.install_path: item for item in receipt.files}
    actions = []
    for item in desired.files:
        old = old_by_target.get(item.install_path)
        if old is None:
            if _target_exists_without_symlink(root, item.install_path):
                raise WikiKitError("kit_unmanaged_target")
            actions.append(UpgradeAction(item.source_path, item.install_path, item.sha256, None))
        elif old.sha256 != item.sha256:
            actions.append(UpgradeAction(
                item.source_path, item.install_path, item.sha256, old.sha256))
    return desired, tuple(actions)

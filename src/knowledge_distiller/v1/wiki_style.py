"""Independent installation and activation of the managed kd-wiki snippet."""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path

from .wiki_kit import WikiKitError, read_receipt, verify_installed_kit
from .wiki_kit_install import (
    FileMutation,
    WikiKitInstallError,
    _MutationService,
    _read,
    apply_transaction,
    journal_pending,
    recover_transaction,
)
from .wiki_lock import WikiLockError, canonical_vault


ASSET_PATH = ".kd/assets/kd-wiki.css"
SNIPPET_PATH = ".obsidian/snippets/kd-wiki.css"
STYLE_RECEIPT_PATH = ".kd/wiki-style.json"
APPEARANCE_PATH = ".obsidian/appearance.json"
SNIPPET_NAME = "kd-wiki"


class WikiStyleError(RuntimeError):
    """A fixed-code style operation failure."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class StyleStatus:
    state: str
    action: str | None
    error_code: str | None = None


@dataclass(frozen=True)
class StyleInstallResult:
    state: str
    changed_paths: tuple[str, ...] = ()


def _sha(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _asset(vault) -> tuple[bytes, str]:
    try:
        receipt = read_receipt(vault)
        if receipt is None:
            raise WikiStyleError("kit_missing")
        verify_installed_kit(vault, receipt)
        owned = next((item for item in receipt.files if item.install_path == ASSET_PATH), None)
        if owned is None:
            raise WikiStyleError("style_asset_unavailable")
        current = _read(canonical_vault(vault), ASSET_PATH)
        if current is None or _sha(current[0]) != owned.sha256:
            raise WikiStyleError("kit_drift")
        return current[0], owned.sha256
    except WikiStyleError:
        raise
    except (WikiKitError, WikiKitInstallError) as error:
        raise WikiStyleError(str(error)) from error


def _receipt_bytes(asset_sha: str) -> bytes:
    return (json.dumps({"receipt_version": 1, "asset_sha256": asset_sha,
                        "snippet_sha256": asset_sha, "install_path": SNIPPET_PATH},
                       sort_keys=True) + "\n").encode()


def _style_receipt(vault) -> dict | None:
    current = _read(canonical_vault(vault), STYLE_RECEIPT_PATH)
    if current is None:
        return None
    try:
        data = json.loads(current[0])
    except (UnicodeError, json.JSONDecodeError) as error:
        raise WikiStyleError("style_receipt_invalid") from error
    if (not isinstance(data, dict) or set(data) != {
            "receipt_version", "asset_sha256", "snippet_sha256", "install_path"}
            or data["receipt_version"] != 1 or data["install_path"] != SNIPPET_PATH
            or not all(isinstance(data[name], str) and len(data[name]) == 64
                       for name in ("asset_sha256", "snippet_sha256"))):
        raise WikiStyleError("style_receipt_invalid")
    return data


def _appearance(vault) -> tuple[dict, int, bool, str | None]:
    current = _read(canonical_vault(vault), APPEARANCE_PATH)
    if current is None:
        return {}, 0o600, False, None
    try:
        data = json.loads(current[0])
    except (UnicodeError, json.JSONDecodeError) as error:
        raise WikiStyleError("appearance_invalid") from error
    if not isinstance(data, dict):
        raise WikiStyleError("appearance_invalid")
    enabled = data.get("enabledCssSnippets", [])
    if (not isinstance(enabled, list) or any(not isinstance(item, str) for item in enabled)):
        raise WikiStyleError("appearance_invalid")
    return data, current[1], True, _sha(current[0])


class WikiStyleService(_MutationService):
    def __init__(self, runtime_root, gate, coordinator):
        super().__init__(runtime_root, gate, coordinator, "wiki-style")

    def status(self, vault) -> StyleStatus:
        try:
            if journal_pending(self.journal_root(vault)):
                return StyleStatus("recovery_required", "recover", "recovery_required")
            _content, asset_sha = _asset(vault)
            receipt = _style_receipt(vault)
            snippet = _read(canonical_vault(vault), SNIPPET_PATH)
            if receipt is None:
                if snippet is not None:
                    return StyleStatus("conflict", None, "style_unmanaged_target")
                return StyleStatus("missing", "install")
            if snippet is None or _sha(snippet[0]) != receipt["snippet_sha256"]:
                return StyleStatus("conflict", None, "style_drift")
            if receipt["asset_sha256"] != asset_sha:
                return StyleStatus("update_available", "update")
            appearance, _mode, _exists, _digest = _appearance(vault)
            enabled = SNIPPET_NAME in appearance.get("enabledCssSnippets", [])
            return StyleStatus("enabled" if enabled else "installed",
                               "disable" if enabled else "enable")
        except WikiStyleError as error:
            code = str(error)
            if code in {"kit_missing", "style_asset_unavailable", "kit_drift"}:
                return StyleStatus("asset_unavailable", None, code)
            return StyleStatus("conflict", None, code)
        except (WikiKitInstallError, WikiLockError) as error:
            return StyleStatus("conflict", None, str(error))
        except OSError:
            return StyleStatus("conflict", None, "style_read_failed")

    def install(self, vault) -> StyleInstallResult:
        def operation(lock):
            asset, asset_sha = _asset(vault)
            receipt = _style_receipt(vault)
            snippet = _read(canonical_vault(vault), SNIPPET_PATH)
            if receipt is None and snippet is not None:
                raise WikiStyleError("style_unmanaged_target")
            if receipt is not None and (snippet is None or _sha(snippet[0]) != receipt["snippet_sha256"]):
                raise WikiStyleError("style_drift")
            wanted = _receipt_bytes(asset_sha)
            current_receipt = _read(canonical_vault(vault), STYLE_RECEIPT_PATH)
            mutations = [FileMutation(
                SNIPPET_PATH, asset, 0o644, _sha(snippet[0]) if snippet else None),
                FileMutation(STYLE_RECEIPT_PATH, wanted, 0o600,
                             _sha(current_receipt[0]) if current_receipt else None)]
            changed = apply_transaction(vault, self.journal_root(vault), mutations,
                                        lock=lock, kind="wiki-style-install")
            state = "unchanged" if not changed else ("installed" if receipt is None else "updated")
            return StyleInstallResult(state, changed)
        try:
            return self.mutate(vault, operation)
        except WikiStyleError:
            raise

    def enable(self, vault) -> StyleInstallResult:
        return self._set_enabled(vault, True)

    def disable(self, vault) -> StyleInstallResult:
        return self._set_enabled(vault, False)

    def _set_enabled(self, vault, enabled: bool) -> StyleInstallResult:
        def operation(lock):
            state = self.status(vault)
            if state.state not in {"installed", "enabled"}:
                raise WikiStyleError(state.error_code or "style_not_installed")
            appearance, mode, existed, appearance_digest = _appearance(vault)
            values = list(appearance.get("enabledCssSnippets", []))
            present = SNIPPET_NAME in values
            if present == enabled:
                return StyleInstallResult("enabled" if enabled else "installed")
            if enabled:
                values.append(SNIPPET_NAME)
            else:
                values = [item for item in values if item != SNIPPET_NAME]
            appearance["enabledCssSnippets"] = values
            content = (json.dumps(appearance, ensure_ascii=False, indent=2) + "\n").encode()
            changed = apply_transaction(vault, self.journal_root(vault),
                                        [FileMutation(APPEARANCE_PATH, content,
                                                      mode if existed else 0o600,
                                                      appearance_digest)],
                                        lock=lock, kind="wiki-style-appearance")
            return StyleInstallResult("enabled" if enabled else "installed", changed)
        try:
            return self.mutate(vault, operation)
        except WikiStyleError:
            raise

    def recover(self, vault) -> StyleInstallResult:
        def operation(lock):
            state = recover_transaction(
                vault, self.journal_root(vault), lock=lock,
                allowed_kinds={"wiki-style-install", "wiki-style-appearance"})
            return StyleInstallResult(state)
        return self.mutate(vault, operation)

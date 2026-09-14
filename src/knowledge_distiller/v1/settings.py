from __future__ import annotations

import json
from knowledge_distiller.v1.model_json import parse_model_json
import logging
import re
import sqlite3
import subprocess
import sys
from pathlib import Path
from typing import Callable
from urllib.parse import urlsplit
from uuid import uuid4

from knowledge_distiller.primary import QWEN_MODEL_ID

from .chrome import ChromeSessionError, DouyinChromeSession
from .keychain import KeychainError, credential_label
from .store import Store


logger = logging.getLogger(__name__)

PLATFORMS = (
    ("douyin", "抖音"),
    ("xiaohongshu", "小红书"),
    ("weibo", "微博"),
    ("zhihu", "知乎"),
    ("youtube", "YouTube"),
    ("x", "X"),
)


ASR_FAILURE_TEXT = {
    "doubao_tos_invalid_key": "TOS Access Key ID 无效，请检查与 Secret Access Key 是否为同一组有效凭据。",
    "doubao_tos_access_denied": "TOS 拒绝访问，请检查当前密钥对所选存储桶的访问权限和桶策略。",
    "doubao_api_unavailable": "豆包转写服务拒绝认证，请检查 API Key 与服务开通状态。",
    "doubao_credentials_unavailable": "豆包或 TOS 凭据无法读取，请重新保存并启用。",
    "doubao_runtime_unavailable": "豆包识别运行组件不可用。",
}


class SettingsError(RuntimeError):
    pass


KeychainFactory = Callable[[str], object]
VaultPicker = Callable[[], Path | None]


class SettingsService:
    def __init__(
        self,
        store: Store,
        *,
        chrome: DouyinChromeSession | None = None,
        keychain_factory: KeychainFactory | None = None,
        vault_picker: VaultPicker | None = None,
        qwen_probe: Callable[[], bool] | None = None,
        qwen_component=None,
        slot_id: Callable[[], str] | None = None,
        codex_probe=None,
        codex_client=None,
        youtube=None,
        xiaohongshu=None,
        xpost=None,
        zhihu=None,
        weibo=None,
    ):
        from .platform_sessions import PlatformOwnedSession
        def owned(platform):
            return PlatformOwnedSession(store, store.path.parent / 'browser-profiles' / platform, platform)
        self.youtube = youtube or owned('youtube')
        from .foreground_session import PlatformForegroundSession
        self.xiaohongshu = xiaohongshu or PlatformForegroundSession(store, store.path.parent / 'browser-profiles' / 'xiaohongshu', 'xiaohongshu')
        self.xpost = xpost or owned('x')
        from .zhihu_session import ZhihuForegroundSession
        self.zhihu = zhihu or ZhihuForegroundSession(store, store.path.parent / 'browser-profiles' / 'zhihu')
        self.weibo = weibo or owned('weibo')
        from .codex import subscription_models, CodexSubscriptionClient
        self.codex_probe = codex_probe or subscription_models
        self.codex_client = codex_client or CodexSubscriptionClient
        self.store = store
        self.chrome = chrome or owned('douyin')
        from .local_secrets import LocalSecrets
        self.local_secrets = LocalSecrets(store.path.parent / "credentials")
        self.keychain_factory = keychain_factory or self.local_secrets
        self.vault_picker = vault_picker or choose_vault
        from .qwen_component import QwenComponent
        self.qwen_component = qwen_component or QwenComponent(store.path.parent / "components" / "qwen")
        self.qwen_probe = qwen_probe or (lambda: self.qwen_component.supported and self.qwen_component.ready())
        self.slot_id = slot_id or (lambda: uuid4().hex)
        self._labelled_accounts: set[str] = set()

    def sync_credential_labels(self) -> None:
        values = self.store.settings()
        labels = {}
        for key, active, detail in (
            ("llm_draft_secret_account", False, ""),
            ("seed_draft_api_account", False, ""),
            ("seed_draft_tos_account", False, values.get("seed_draft_bucket", "")),
            ("llm_secret_account", values.get("llm_provider") != "codex", values.get("llm_model", "")),
            ("asr_seed_api_account", values.get("asr_model") == "volc.seedasr.auc", ""),
            ("asr_seed_tos_account", values.get("asr_model") == "volc.seedasr.auc", values.get("asr_seed_bucket", "")),
        ):
            account = values.get(key)
            if account:
                labels[account] = credential_label(account, "当前启用" if active else "已保存未启用", detail)
        current_accounts = set(labels)
        for account in self._labelled_accounts - labels.keys():
            labels[account] = credential_label(account, "已替换")
        for account, label in labels.items():
            secret = self.keychain_factory(account)
            # Injected test/embedding backends may not expose macOS metadata.
            if not hasattr(secret, "set_label"):
                continue
            try:
                secret.set_label(label)
            except KeychainError:
                logger.warning("Credential label update failed; credential binding unchanged")
        self._labelled_accounts = current_accounts


    def _credential_saved(self, account):
        if not account: return False
        if self.keychain_factory is not self.local_secrets: return True
        return self.local_secrets.status(account) in {'pending_validation', 'validated'}

    def credential_view(self):
        try:
            self.local_secrets._directory()
            state = 'configured'
        except KeychainError:
            state = 'problem'
        return {'path': str(self.local_secrets.root), 'state': state}

    def local_address_view(self):
        from .local_address import load
        address = load(self.store.path.parent)
        return {'name': address.name, 'port': address.port, 'host': address.host, 'url': address.url}

    def view(self) -> dict[str, object]:
        values = self.store.settings()
        connections = self.store.connections()
        platforms = []
        for key, label in PLATFORMS:
            row = connections.get(key)
            platforms.append(
                {
                    "key": key,
                    "label": label,
                    "state": ("relogin_required" if row is not None
                              and row["state"] == "connected" and hasattr({'douyin': self.chrome, 'youtube': self.youtube, 'xiaohongshu': self.xiaohongshu, 'x': self.xpost, 'zhihu': self.zhihu, 'weibo': self.weibo}[key], 'browser_page')
                              and not (row["browser_context"] or '').startswith('owned:')
                              else row["state"] if row is not None else "unconfigured"),
                    "account_label": row["account_label"] if row is not None else None,
                    "connection_mode": ('owned' if (row['browser_context'] or '').startswith('owned:') else 'foreground') if key in {'zhihu', 'xiaohongshu'} and row is not None and row['state'] != 'unconfigured' else None,
                    "pending_login": bool(values.get(key + "_pending_login")),
                    "available": key in {"douyin", "youtube", "xiaohongshu", "x", "zhihu", "weibo"},
                }
            )

        llm_configured = all(
            values.get(key)
            for key in ("llm_base_url", "llm_model", "llm_secret_account")
        )
        codex_active = values.get("llm_provider") == "codex"
        if codex_active:
            llm_configured = bool(values.get("llm_model"))
        llm_state = values.get("llm_state") if llm_configured else "unconfigured"
        asr_configured = values.get("asr_model") == QWEN_MODEL_ID
        seed_active = values.get("asr_model") == "volc.seedasr.auc"
        asr_configured = asr_configured or seed_active
        asr_state = values.get("asr_state") if asr_configured else "unconfigured"
        if values.get("asr_model") == QWEN_MODEL_ID and not self.qwen_probe():
            asr_state = "unavailable"
        if not codex_active and llm_configured and not self._credential_saved(values.get('llm_secret_account')):
            llm_state = 'unavailable'
        if seed_active and not all(self._credential_saved(values.get(key)) for key in ('asr_seed_api_account', 'asr_seed_tos_account')):
            asr_state = 'unavailable'
        vault_value = values.get("vault_path")
        from .vault_access import vault_status
        vault_access = vault_status(vault_value)
        vault_state = vault_access["state"]
        return {
            "local_address": self.local_address_view(),
            "credentials": self.credential_view(),
            "platforms": platforms,
            "cleanup_residue_count": sum(key.startswith("temporary_cleanup_item_") or key == 'platform_media_cleanup' for key in values),
            "llm": {
                "state": llm_state or "configured",
                "provider": ("Codex" if codex_active else "OpenAI API" if values.get("llm_provider") == "openai" else "Anthropic API（旧配置）") if llm_configured else "-",
                "provider_id": "codex" if codex_active else "openai",
                "effort": values.get("llm_effort", ""),
                "service_tier": values.get("llm_service_tier", "") if codex_active else "",
                "codex_models": json.loads(values.get("codex_models", "[]")),
                "api_key_saved": self._credential_saved(values.get("llm_draft_secret_account")) and values.get("llm_draft_provider") == "openai",
                "model": values.get("llm_model", ""),
                "base_url": values.get("llm_base_url", "https://api.openai.com") if values.get("llm_provider") == "openai" else "https://api.openai.com",
            },
            "asr": {
                "component": self.qwen_component.status(),
                "state": asr_state or "configured",
                "provider": ("豆包" if seed_active else "Qwen") if asr_configured else "-",
                "model": ("豆包录音文件识别模型 2.0" if seed_active else "Qwen3-ASR 1.7B") if asr_configured else "",
                "provider_id": "doubao" if seed_active else "qwen",
                "failure_message": ASR_FAILURE_TEXT.get(values.get("asr_failure_reason"), "") if asr_state == "unavailable" else "",
                "api_key_saved": self._credential_saved(values.get("seed_draft_api_account")),
                "tos_saved": self._credential_saved(values.get("seed_draft_tos_account")) and all(values.get(k) for k in ('seed_draft_tos_account','seed_draft_region','seed_draft_bucket')),
                "tos_key_saved": self._credential_saved(values.get('seed_draft_tos_account')),
                "storage_discovery": json.loads(values.get('seed_storage_discovery') or '{}'),
                "region": values.get("seed_draft_region", ""),
                "bucket": values.get("seed_draft_bucket", ""),
            },
            "source_files": {"path": str(self.store.path.parent.resolve() / "source-files")},
            "vault": {
                **vault_access,
                "state": vault_state,
                "path": vault_value or "-",
                "action": {
                    "configured": "更换位置",
                    "unconfigured": "选择位置",
                    "problem": "修改位置",
                }[vault_state],
            },
        }

    def open_source_files(self) -> None:
        from .source_files import open_directory, SourceCopyError
        try:
            open_directory(self.store.path.parent)
        except SourceCopyError as error:
            raise SettingsError("source_files_open_failed") from error

    def connect_douyin(self) -> None:
        old = self.store.connection("douyin")
        connection = self.chrome.verify()
        context = getattr(connection, "browser_context", None)
        try:
            self.store.save_connection("douyin", connection.account_label, browser_context=context)
        except Exception:
            if hasattr(self.chrome, 'discard'):
                self.chrome.discard(context)
            raise
        if old and hasattr(self.chrome, 'discard'):
            self.chrome.discard(old['browser_context'])

    def clear_douyin(self) -> None:
        old = self.store.connection("douyin")
        self.store.clear_connection("douyin")
        if old and hasattr(self.chrome, 'discard'):
            self.chrome.discard(old['browser_context'])

    def _connect_owned(self, platform, session):
        if not hasattr(session, 'discard'):
            return False
        old = self.store.connection(platform)
        result = session.verify()
        try:
            self.store.save_connection(platform, result.account_label, browser_context=result.browser_context)
        except Exception:
            session.discard(result.browser_context)
            raise
        if hasattr(session, 'login_committed'):
            session.login_committed()
        if old and old['browser_context'] != result.browser_context:
            session.discard(old['browser_context'])
        return True

    def _clear_owned(self, platform, session):
        old = self.store.connection(platform)
        if hasattr(session, 'cancel_login'):
            session.cancel_login()
        self.store.clear_connection(platform)
        if old and hasattr(session, 'discard'):
            session.discard(old['browser_context'])

    def cancel_platform_login(self, platform):
        session = {'youtube': self.youtube, 'x': self.xpost}.get(platform)
        if session is None or not hasattr(session, 'cancel_login'):
            raise ChromeSessionError('chrome_connection_failed')
        session.cancel_login()

    def connect_zhihu(self) -> None:
        if self._connect_owned('zhihu', self.zhihu):
            return
        self.store.save_connection('zhihu', None, browser_context=self.zhihu.verify())

    def clear_zhihu(self) -> None:
        self._clear_owned('zhihu', self.zhihu)

    def connect_weibo(self) -> None:
        if self._connect_owned('weibo', self.weibo):
            return
        self.store.save_connection('weibo', None, browser_context=self.weibo.verify())

    def clear_weibo(self) -> None:
        self._clear_owned('weibo', self.weibo)

    def connect_x(self) -> None:
        if self._connect_owned('x', self.xpost):
            return
        self.store.save_connection('x', None, browser_context=self.xpost.verify())

    def clear_x(self) -> None:
        self._clear_owned('x', self.xpost)

    def connect_xiaohongshu(self) -> None:
        if self._connect_owned('xiaohongshu', self.xiaohongshu):
            return
        context = self.xiaohongshu.verify()
        self.store.save_connection('xiaohongshu', None, browser_context=context)

    def clear_xiaohongshu(self) -> None:
        self._clear_owned('xiaohongshu', self.xiaohongshu)

    def connect_youtube(self) -> None:
        if self._connect_owned('youtube', self.youtube):
            return
        connection = self.youtube.verify()
        self.store.save_connection("youtube", connection.account_label)

    def clear_youtube(self) -> None:
        self._clear_owned('youtube', self.youtube)

    def activate_llm(self, base_url: str, model: str, secret: str) -> None:
        normalized_url = _llm_url(base_url)
        normalized_model = _model_id(model)
        old_account = self.store.setting("llm_secret_account")
        new_account = f"llm-api-key-{self.slot_id()}"
        candidate = self.keychain_factory(new_account)
        try:
            candidate.save(secret)
            self.store.set_settings(
                {
                    "llm_provider": "openai",
                    "llm_effort": "",
                    "llm_base_url": normalized_url,
                    "llm_model": normalized_model,
                    "llm_secret_account": new_account,
                    "llm_state": "configured",
                }
            )
        except (KeychainError, OSError, ValueError, sqlite3.Error) as error:
            try:
                candidate.clear()
            except KeychainError:
                pass
            raise SettingsError("llm_save_failed") from error

        self.sync_credential_labels()
        if old_account and old_account != new_account:
            try:
                self.keychain_factory(old_account).clear()
            except KeychainError:
                logger.warning("Inactive LLM Keychain item could not be removed")

    def _save_draft_secret(self, key: str, secret: str, extra=None) -> None:
        if not secret.strip():
            raise SettingsError("model_credentials_invalid")
        account = f"{key}-{self.slot_id()}"
        candidate = self.keychain_factory(account)
        old = self.store.setting(key)
        try:
            candidate.save(secret)
            self.store.set_settings({key: account, **(extra or {})})
        except (KeychainError, OSError, ValueError, sqlite3.Error) as error:
            try:
                candidate.clear()
            except KeychainError:
                pass
            raise SettingsError("model_credentials_save_failed") from error
        self.sync_credential_labels()
        if old and old not in self.store.settings().values():
            try:
                self.keychain_factory(old).clear()
            except KeychainError:
                logger.warning("Inactive credential could not be removed")

    def save_llm_key(self, secret: str) -> None:
        self._save_draft_secret("llm_draft_secret_account", secret, {"llm_draft_provider": "openai"})

    def activate_saved_api(self, base_url: str, model: str) -> None:
        account = self.store.setting("llm_draft_secret_account")
        if self.store.setting("llm_draft_provider") != "openai":
            raise SettingsError("model_credentials_invalid")
        try:
            secret = self.keychain_factory(account).load() if account else ""
        except KeychainError as error:
            raise SettingsError("model_credentials_invalid") from error
        if not secret:
            raise SettingsError("model_credentials_invalid")
        self.activate_llm(base_url, model, secret)

    def save_doubao_key(self, secret: str) -> None:
        self._save_draft_secret("seed_draft_api_account", secret)

    def save_doubao_tos(self, region: str, bucket: str, access_key: str, secret_key: str) -> None:
        region, bucket = region.strip(), bucket.strip()
        if (not re.fullmatch(r"[a-z]{2}-[a-z]+(?:-\d+)?", region)
                or not re.fullmatch(r"[a-z0-9][a-z0-9-]{1,61}[a-z0-9]", bucket)
                or not access_key.strip() or not secret_key.strip()):
            raise SettingsError("model_credentials_invalid")
        self._save_draft_secret("seed_draft_tos_account",
            json.dumps({"access_key": access_key, "secret_key": secret_key}),
            {"seed_draft_region": region, "seed_draft_bucket": bucket, "seed_storage_discovery": ""})

    def activate_doubao(self) -> None:
        values = self.store.settings()
        required = ("seed_draft_api_account", "seed_draft_tos_account", "seed_draft_region", "seed_draft_bucket")
        if not all(values.get(key) for key in required):
            raise SettingsError("model_credentials_invalid")
        try:
            api = self.keychain_factory(values["seed_draft_api_account"]).load()
            tos = json.loads(self.keychain_factory(values["seed_draft_tos_account"]).load())
            if not api or not tos.get("access_key") or not tos.get("secret_key"):
                raise ValueError("incomplete credentials")
            import tos as tos_sdk
        except (KeychainError, ValueError, TypeError) as error:
            raise SettingsError("model_credentials_invalid") from error
        except ImportError as error:
            raise SettingsError("doubao_runtime_unavailable") from error
        self.store.set_settings({"asr_model": "volc.seedasr.auc", "asr_state": "configured", "asr_failure_reason": "",
            **{key.replace("seed_draft_", "asr_seed_"): values[key] for key in required}})
        self.sync_credential_labels()

    def doubao_secret(self, kind: str, values=None) -> str:
        values = self.store.settings() if values is None else values
        key = "asr_seed_api_account" if kind == "api_key" else "asr_seed_tos_account"
        try:
            account = values.get(key)
            if not account:
                raise KeychainError("missing")
            value = self.keychain_factory(account).load()
            return value if kind == "api_key" else json.loads(value)[kind]
        except (KeychainError, ValueError, KeyError, TypeError):
            if self.store.setting("asr_seed_api_account") == values.get("asr_seed_api_account") and self.store.setting("asr_seed_tos_account") == values.get("asr_seed_tos_account"):
                self.mark_asr_unavailable()
            return ""

    def refresh_codex(self) -> list[dict]:
        from .llm import LLMRequestError
        try:
            models = self.codex_probe()
        except LLMRequestError as error:
            raise SettingsError("codex_connection_failed") from error
        self.store.set_setting("codex_models", json.dumps(models))
        return models

    def activate_codex(self, model: str, effort: str, service_tier: str = '') -> None:
        from .llm import LLMRequestError
        selected = _model_id(model)
        models = self.refresh_codex()
        known = next((row for row in models if row["model"] == selected), None)
        # Unknown custom IDs retain the model's own default, not guessed efforts.
        if effort and (known is None or effort not in known["efforts"]):
            raise SettingsError("codex_effort_invalid")
        if service_tier not in {'', 'fast'} or (service_tier == 'fast' and not (known and known.get('fast_supported'))):
            raise SettingsError("codex_fast_unavailable")
        try:
            answer = self.codex_client(selected, effort, service_tier=service_tier).complete(
                system='Return exactly {"connected":true}.', user='Connection check.', max_tokens=32)
            if parse_model_json(answer).value != {"connected": True}:
                raise ValueError("invalid connection response")
        except (LLMRequestError, ValueError) as error:
            raise SettingsError("codex_connection_failed") from error
        self.store.set_settings({"llm_provider": "codex", "llm_model": selected,
                                 "llm_effort": effort, "llm_service_tier": service_tier, "llm_state": "configured"})
        self.sync_credential_labels()

    def llm_client(self):
        from .llm import AnthropicMessagesClient, OpenAIResponsesClient
        values = self.store.settings()
        if values.get("llm_provider") == "codex":
            return self.codex_client(values.get("llm_model", ""), values.get("llm_effort", ""),
                                     self.mark_llm_unavailable, service_tier=values.get("llm_service_tier", ""))
        client_type = OpenAIResponsesClient if values.get("llm_provider") == "openai" else AnthropicMessagesClient
        return client_type(values.get("llm_base_url", ""),
            values.get("llm_model", ""), self.llm_secret, self.mark_llm_unavailable)

    def activate_asr(self) -> None:
        if not self.qwen_probe():
            raise SettingsError("asr_runtime_unavailable")
        self.store.set_settings(
            {"asr_model": QWEN_MODEL_ID, "asr_state": "configured", "asr_failure_reason": ""}
        )

        self.sync_credential_labels()

    def choose_vault(self) -> bool:
        selected = self.vault_picker()
        if selected is None:
            return False
        try:
            resolved = selected.resolve(strict=True)
        except (OSError, RuntimeError) as error:
            raise SettingsError("vault_invalid") from error
        if not resolved.is_dir():
            raise SettingsError("vault_invalid")
        # Only the explicit selection action probes write permission, never GET.
        import tempfile
        try:
            with tempfile.TemporaryFile(prefix='.kd-write-check-', dir=resolved) as probe:
                probe.write(b'check')
                probe.flush()
        except OSError as error:
            raise SettingsError("vault_not_writable") from error
        self.store.set_setting("vault_path", str(resolved))
        return True

    def llm_secret(self) -> str:
        account = self.store.setting("llm_secret_account")
        if not account:
            return ""
        try:
            return self.keychain_factory(account).load()
        except KeychainError:
            self.store.set_setting("llm_state", "unavailable")
            return ""

    def mark_llm_unavailable(self) -> None:
        if self.store.setting("llm_model"):
            self.store.set_setting("llm_state", "unavailable")

    def mark_asr_unavailable(self, reason: str = "") -> None:
        if self.store.setting("asr_model"):
            self.store.set_settings({"asr_state": "unavailable", "asr_failure_reason": reason if reason in ASR_FAILURE_TEXT else ""})


class AuthorizedDouyinSession:
    def __init__(self, store: Store, chrome: DouyinChromeSession):
        self.store = store
        self.chrome = chrome

    def cookies(self):
        connection = self.store.connection("douyin")
        if connection is None or connection["state"] == "unconfigured":
            raise ChromeSessionError("douyin_not_configured")
        if connection["state"] == "relogin_required":
            raise ChromeSessionError("douyin_login_required")
        try:
            return self.chrome.cookies()
        except ChromeSessionError as error:
            if error.args and error.args[0] == "douyin_login_required":
                self.store.require_relogin("douyin")
            raise


def choose_vault() -> Path | None:
    if sys.platform == 'win32':
        from .desktop_paths import choose_windows_folder
        try:
            return choose_windows_folder()
        except OSError as error:
            raise SettingsError("vault_picker_failed") from error
    result = subprocess.run(
        [
            "/usr/bin/osascript",
            "-e",
            'POSIX path of (choose folder with prompt "选择 Obsidian Vault")',
        ],
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        if "User canceled" in result.stderr or "-128" in result.stderr:
            return None
        raise SettingsError("vault_picker_failed")
    value = result.stdout.strip().rstrip("/")
    if not value:
        raise SettingsError("vault_picker_failed")
    return Path(value)


def _llm_url(value: str) -> str:
    candidate = value.strip().rstrip("/")
    parsed = urlsplit(candidate)
    local_http = parsed.scheme == "http" and parsed.hostname in {
        "127.0.0.1",
        "localhost",
    }
    if (
        (parsed.scheme != "https" and not local_http)
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise SettingsError("llm_config_invalid")
    return candidate


def _model_id(value: str) -> str:
    candidate = value.strip()
    if (
        not candidate
        or len(candidate) > 128
        or "\n" in candidate
        or "\r" in candidate
    ):
        raise SettingsError("llm_config_invalid")
    return candidate


def _vault_state(value: str | None) -> str:
    from .vault_access import vault_status
    return vault_status(value)['state']

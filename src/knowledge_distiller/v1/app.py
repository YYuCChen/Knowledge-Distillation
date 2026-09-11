from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from knowledge_distiller.primary import (
    FFmpegAudioNormalizer,
    PrimaryFailure,
    PrimaryRecognition,
    QWEN_MODEL_ID,
    QwenPrimaryAdapter,
)

from .chrome import DouyinChromeSession
from .confirmation import FFmpegConfirmationClipper
from .douyin import DouyinSource
from .knowledge_model import AnthropicKnowledgeModel
from .pipeline import Distiller
from .reviewer import build_reviewer
from .settings import AuthorizedDouyinSession, SettingsService
from .store import Store
from .web import create_app
from .worker import SingleWorker
from .youtube import YouTubeSource
from .bilibili import BilibiliSource
from .xiaohongshu import XiaohongshuSource
from .xpost import XPostSource
from .zhihu import ZhihuSource
from .weibo import WeiboSource


@dataclass(frozen=True)
class AppPaths:
    data_root: Path

    @property
    def database(self) -> Path:
        return self.data_root / "knowledge.sqlite3"

    @property
    def runtime(self) -> Path:
        return self.data_root / "runtime"

    @classmethod
    def mac_default(cls) -> AppPaths:
        return cls(
            Path.home()
            / "Library"
            / "Application Support"
            / "Knowledge Distiller"
        )


def create_application(
    paths: AppPaths | None = None,
    *,
    chrome: DouyinChromeSession | None = None,
    settings: SettingsService | None = None,
    start_workers: bool = True,
):
    selected_paths = paths or AppPaths.mac_default()
    store = Store(selected_paths.database)
    from .douyin_session import DouyinOwnedSession
    browser = chrome or DouyinOwnedSession(store, selected_paths.data_root / "browser-profiles" / "douyin")
    settings_service = settings or SettingsService(store, chrome=browser)
    authorized_browser = AuthorizedDouyinSession(store, browser)
    from .ocr import default_ocr_runner
    from .docling_source import DoclingSourceConverter
    local_ocr = default_ocr_runner()
    local_documents = DoclingSourceConverter()

    def build_distiller() -> Distiller:
        values = store.settings()
        client = settings_service.llm_client()
        return Distiller(
            store=store,
            ocr=local_ocr,
            documents=local_documents,
            source=DouyinSource(authorized_browser),
            youtube_source=YouTubeSource(store, settings_service.youtube),
            bilibili_source=BilibiliSource(),
            xiaohongshu_source=XiaohongshuSource(store, settings_service.xiaohongshu),
            xpost_source=XPostSource(store, settings_service.xpost),
            zhihu_source=ZhihuSource(store, settings_service.zhihu),
            weibo_source=WeiboSource(store, settings_service.weibo),
            normalizer=FFmpegAudioNormalizer(),
            recognizer=configured_recognizer(values, settings_service),
            reviewer=build_reviewer(client),
            confirmation_clipper=FFmpegConfirmationClipper(),
            knowledge_model=AnthropicKnowledgeModel(client),
            runtime_root=selected_paths.runtime,
            vault=Path(values["vault_path"]) if values.get("vault_path") else None,
        )

    def build_organization():
        from .organization import configured_organization
        return configured_organization(store, settings_service.llm_client())

    from .temporary_artifacts import TemporaryArtifacts
    worker = SingleWorker(store, build_distiller, organization=build_organization,
                          maintenance=TemporaryArtifacts(store, selected_paths.runtime).sweep)
    app = create_app(
        store,
        build_distiller,
        settings_service,
        wake_worker=worker.wake,
        organization=build_organization,
    )
    from .desktop_pages import install as install_desktop_pages
    install_desktop_pages(app)
    from .feishu_service import FeishuService
    feishu=FeishuService(store,app.extensions['link_intake'],build_distiller,selected_paths.runtime,wake=worker.wake)
    app.extensions['feishu']=feishu
    app.extensions['qwen_component']=settings_service.qwen_component
    app.config['KNOWLEDGE_DISTILLER_CLOSE_FEISHU']=feishu.stop

    def close_browsers():
        feishu.stop()
        settings_service.qwen_component.close()
        for session in (browser, settings_service.youtube, settings_service.xiaohongshu,
                        settings_service.xpost, settings_service.zhihu, settings_service.weibo):
            close = getattr(session, 'close', None)
            if close is not None:
                close()

    app.config["KNOWLEDGE_DISTILLER_CLOSE_BROWSERS"] = close_browsers
    if start_workers:
        worker.start()
        feishu.start()
    else:
        app.extensions['updates'].phase = 'installing'
    app.config["KNOWLEDGE_DISTILLER_STORE"] = store
    app.config["KNOWLEDGE_DISTILLER_WORKER"] = worker
    return app


class ConfiguredQwenRecognizer:
    def __init__(self, model: str | None, settings: SettingsService):
        self._settings = settings
        self.model = model
        from .qwen_component import ComponentQwenRuntime
        self._recognizer = QwenPrimaryAdapter(ComponentQwenRuntime(settings.qwen_component)) if model == QWEN_MODEL_ID else None

    @property
    def cache_identity(self):
        from . import qwen_component
        return {'provider': 'qwen', 'model': self.model, 'enabled': self._recognizer is not None,
                'revision': qwen_component.QWEN_MODEL_REVISION,
                'runtime': qwen_component.QWEN_RUNTIME_VERSION}

    def recognize(self, audio) -> PrimaryRecognition:
        if self._recognizer is None:
            return PrimaryRecognition.failed(PrimaryFailure.RUNTIME_UNAVAILABLE)
        result = self._recognizer.recognize(audio)
        if result.failure is PrimaryFailure.RUNTIME_UNAVAILABLE:
            if self._settings.store.setting("asr_model") == QWEN_MODEL_ID:
                self._settings.mark_asr_unavailable()
        return result


def configured_recognizer(values, settings):
    if values.get("asr_model") == "volc.seedasr.auc":
        from .doubao_asr import DoubaoRecognizer
        def mark_unavailable(reason=""):
            current = settings.store.settings()
            if all(current.get(key) == values.get(key) for key in ("asr_model", "asr_seed_api_account", "asr_seed_tos_account")):
                settings.mark_asr_unavailable(reason)
        return DoubaoRecognizer(
            api_key=lambda: settings.doubao_secret("api_key", values),
            access_key=lambda: settings.doubao_secret("access_key", values),
            secret_key=lambda: settings.doubao_secret("secret_key", values),
            region=values.get("asr_seed_region", ""), bucket=values.get("asr_seed_bucket", ""),
            mark_unavailable=mark_unavailable, report_unavailable=mark_unavailable)
    return ConfiguredQwenRecognizer(values.get("asr_model"), settings)

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
from .worker_lifecycle import WorkAdmissionGate, WorkerCoordinator
from .wiki_kit_runtime import WikiKitRuntime
from .wiki_kit_install import WikiKitInstaller
from .wiki_lock import canonical_vault
from .wiki_runner import CodexWikiRunner
from .wiki_style import WikiStyleService
from .wiki_tasks import WikiTaskStore
from .wiki_worker import WikiWorker
from .wiki_workflow import WikiWorkflow
from .youtube import YouTubeSource
from .bilibili import BilibiliSource
from .xiaohongshu import XiaohongshuSource
from .xpost import XPostSource
from .zhihu import ZhihuSource
from .weibo import WeiboSource


from .paths import AppPaths

def create_application(
    paths: AppPaths | None = None,
    *,
    chrome: DouyinChromeSession | None = None,
    settings: SettingsService | None = None,
    start_workers: bool = True,
):
    selected_paths = paths or AppPaths.system_default()
    selected_paths.runtime.mkdir(mode=0o700, parents=True, exist_ok=True)
    canonical_vault(selected_paths.runtime)
    selected_paths.runtime.chmod(0o700)
    store = Store(selected_paths.database)
    from .douyin_session import DouyinOwnedSession
    browser = chrome or DouyinOwnedSession(store, selected_paths.data_root / "browser-profiles" / "douyin")
    settings_service = settings or SettingsService(store, chrome=browser)
    authorized_browser = AuthorizedDouyinSession(store, browser)
    from .ocr import default_ocr_runner
    from .docling_source import DoclingSourceConverter
    local_ocr = default_ocr_runner()
    local_documents = DoclingSourceConverter(components_root=selected_paths.data_root / 'components')

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

    from .temporary_artifacts import TemporaryArtifacts
    artifacts = TemporaryArtifacts(store, selected_paths.runtime)
    workflow_holder = {}

    def maintenance():
        # Backfill raw/ first: a material's media is released only after its raw
        # file was written (media_lifecycle.RELEASABLE). Quick notes waiting for
        # earlier deliveries' raw ids are written once those settle.
        from .raw import RawLedger
        from .captures import Captures
        import logging
        for step in (lambda: RawLedger(store).write_pending(),
                     lambda: Captures(store, jev=settings_service.jev_client).write_ready()):
            try:
                step()
            except Exception as error:
                logging.getLogger(__name__).warning('raw backfill deferred (%s)', type(error).__name__)
        artifacts.sweep()
        workflow = workflow_holder.get('workflow')
        if workflow is not None:
            workflow.request_refresh()

    worker = SingleWorker(store, build_distiller, maintenance=maintenance)
    kit_runtime = WikiKitRuntime()
    wiki_store = WikiTaskStore(
        selected_paths.database, kit_root=kit_runtime.kit_root,
        python_executable=kit_runtime.python_executable, runtime=kit_runtime)
    wiki_runner = CodexWikiRunner(kit_runtime=kit_runtime)
    wiki_worker = WikiWorker(wiki_store, selected_paths.runtime, wiki_runner)
    admission_gate = WorkAdmissionGate()
    coordinator = WorkerCoordinator(worker, wiki_worker, admission_gate)
    wiki_kit_installer = WikiKitInstaller(
        kit_runtime.kit_root,
        selected_paths.runtime,
        admission_gate,
        coordinator,
    )
    wiki_style_service = WikiStyleService(
        selected_paths.runtime,
        admission_gate,
        coordinator,
    )
    wiki_workflow = WikiWorkflow(wiki_store, wiki_worker, store.settings, admission_gate)
    workflow_holder['workflow'] = wiki_workflow
    app = create_app(
        store,
        build_distiller,
        settings_service,
        wake_worker=worker.wake,
        wiki_workflow=wiki_workflow,
        admission_gate=admission_gate,
        wiki_kit_installer=wiki_kit_installer,
        wiki_style_service=wiki_style_service,
    )
    import sys
    if getattr(sys, 'frozen', False):
        local_documents.begin_component_check()
    app.context_processor(lambda: {'document_component': local_documents.readiness})
    app.extensions['document_component'] = local_documents
    # create_app initializes the database. Metadata checks must neither read a
    # missing database during Settings construction nor block the UI startup.
    if start_workers and store.setting('asr_model') == QWEN_MODEL_ID:
        settings_service.qwen_component.begin_legacy_validation()
    from .desktop_pages import install as install_desktop_pages
    install_desktop_pages(app)
    from .feishu_service import FeishuService
    feishu=FeishuService(
        store,app.extensions['link_intake'],build_distiller,selected_paths.runtime,
        wake=worker.wake,jev=settings_service.jev_client,admission_gate=admission_gate)
    coordinator.attach_ingress(feishu)
    app.extensions['feishu']=feishu
    app.extensions['qwen_component']=settings_service.qwen_component
    app.config['KNOWLEDGE_DISTILLER_CLOSE_FEISHU']=feishu.stop

    def close_browsers():
        settings_service.qwen_component.close()
        for session in (browser, settings_service.youtube, settings_service.xiaohongshu,
                        settings_service.xpost, settings_service.zhihu, settings_service.weibo):
            close = getattr(session, 'close', None)
            if close is not None:
                close()

    app.config["KNOWLEDGE_DISTILLER_CLOSE_BROWSERS"] = close_browsers
    if start_workers:
        wiki_workflow.request_refresh(force=True)
        coordinator.start()
    else:
        app.extensions['updates'].phase = 'installing'
    app.config["KNOWLEDGE_DISTILLER_STORE"] = store
    app.config["KNOWLEDGE_DISTILLER_WORKER"] = worker
    app.config["KNOWLEDGE_DISTILLER_WIKI_WORKER"] = wiki_worker
    app.config["KNOWLEDGE_DISTILLER_WORKERS"] = coordinator
    app.extensions['wiki_workflow'] = wiki_workflow
    app.extensions['admission_gate'] = admission_gate
    return app


class ConfiguredQwenRecognizer:
    def __init__(self, model: str | None, settings: SettingsService):
        self._settings = settings
        self.model = model
        from .qwen_component import ComponentQwenRuntime
        self._recognizer = QwenPrimaryAdapter(ComponentQwenRuntime(settings.qwen_component)) if model == QWEN_MODEL_ID else None

    @property
    def cache_identity(self):
        return {'provider': 'qwen', 'model': self.model, 'enabled': self._recognizer is not None,
                'runtime_identity': (self._recognizer.binding.cache_identity
                                     if self._recognizer is not None else None)}

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

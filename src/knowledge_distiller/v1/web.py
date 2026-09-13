from __future__ import annotations

from .vault_access import publication_status, open_saved_location

import json
import re
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Callable
from urllib.parse import quote, urlsplit

from flask import Flask, abort, redirect, render_template, request, send_file, url_for
from .file_sources import prepare_direct_text, prepare_file

from .confirmation_display import english_assistance, local_choices
from .chrome import ChromeSessionError
from .bilibili import BilibiliSourceError
from .pipeline import Distiller
from .settings import SettingsService
from .settings_web import settings_blueprint
from .store import Store


from .intake import URL_RE, LABELS, links_in, platform_for_url, needs_content_choice
ERROR_TEXT = {
    "bilibili_scope_changed": "B 站范围或分段内容已变化，请重新核对范围。已有结果保留。",
    "bilibili_range_too_large": "这个 B 站范围超过本次可完整核对的 200 条上限，请选择更小范围或分段链接。未提交部分范围。",
    "bilibili_empty": "这个 B 站范围没有可访问的成员，未创建任务。",
    "bilibili_timeout": "读取 B 站范围或下载超时，本次未接受不完整结果，可以重试。",
    "bilibili_login_required": "该 B 站素材需要登录后访问。当前公开采集没有账号权限，未继续处理。",
    "bilibili_payment_required": "该 B 站课程或范围含需要访问权的内容，未将试听或可见部分当作完整来源。",
    "bilibili_unavailable": "B 站素材当前不可访问，可能已删除或受地区限制，请检查原链接。",
    "bilibili_rate_limited": "B 站暂时限制了请求，请稍后重试。已保存结果不受影响。",
    "bilibili_legacy_fragments_unsupported": "这条 B 站来源使用暂未通过完整性验证的旧分片格式，未保存部分音频。",
    "material_source_mismatch": "取得的材料平台与投递来源不一致，本次已停止，请核对链接后重试。",
    "material_metadata_invalid": "来源元数据不完整，未继续识别，请重试采集。",
    "bilibili_scope_required": "这是 B 站多分段或列表来源，需要先确认完整范围。",
    "bilibili_upstream_failed": "B 站内容获取失败，请核对链接是否可访问；受限内容需要有访问权的账号。",
    "bilibili_identity_mismatch": "取得的 B 站素材与提交的分段身份不一致，本次已停止。",
    "bilibili_incomplete": "B 站素材不完整或仍在直播，未继续识别。",
    "bilibili_media_invalid": "B 站音频未通过完整性检查，可以重试。",
    "bilibili_runtime_unavailable": "B 站采集组件尚未就绪，请检查应用安装。",
    "source_platform_unsupported": "无法识别或暂不支持该来源平台，请核对链接。",
    "source_capture_expired": "临时素材已过期，请重试以重新采集。",
    "source_snapshot_changed": "来源内容已变化，原有版本与判断已保留；请重新投递以处理新版本。",
    'zhihu_not_configured': '请先在设置中连接知乎。',
    'zhihu_login_required': '需要重新登录知乎后再试。',
    'zhihu_connection_changed': '知乎连接已变化，请重试以绑定当前连接。',
    'zhihu_identity_mismatch': '取得的知乎内容与提交链接不一致，本次已停止。',
    'zhihu_input_unsupported': '仅支持知乎回答、文章和想法的文本，不采集图片或视频。',
    'zhihu_snapshot_unknown': '无法确认完整的知乎文本快照，本次已停止。',
    'zhihu_text_incomplete': '没有取得完整知乎正文，可以重新尝试。',
    'zhihu_snapshot_changed': '知乎内容变化，原有待处理来源已保留。',
    'zhihu_upstream_failed': '知乎内容获取失败，可以重新尝试。',
    'zhihu_source_unavailable': '当前会话无法读取这条知乎内容，本次已停止。',
    'zhihu_browser_unavailable': '知乎浏览器连接不可用，请检查 OpenCLI 扩展。',
    'zhihu_runtime_unavailable': '知乎采集组件未就绪，请检查 OpenCLI 与 Node.js。',
    'weibo_not_configured': '请先在设置中连接微博。',
    'weibo_login_required': '需要重新登录微博后再试。',
    'weibo_connection_changed': '微博连接已变化，请重试以绑定当前连接。',
    'weibo_identity_mismatch': '取得的微博内容与提交链接不一致，本次已停止。',
    'weibo_input_unsupported': '支持微博文本、长文本和文章，请使用对应的原生链接。',
    'weibo_snapshot_unknown': '无法确认完整的微博文本快照，本次已停止。',
    'weibo_media_unsupported': '这条微博包含图片、视频或附件，超出普通文本来源范围。',
    'weibo_long_text_unsupported': '这条微博尚未取得完整长文，可以重新读取。',
    'weibo_article_restricted': '文章正文受限，未能取得完整内容。',
    'weibo_article_incomplete': '没有取得完整文章正文，可以重新尝试。',
    'weibo_media_incomplete': '文章原图获取不完整，可以重新尝试。',
    'weibo_media_invalid': '文章原图格式无法可靠处理或校验失败。',
    'weibo_nested_only': '这条微博只有转发内容，没有可独立采集的主帖正文。',
    'weibo_text_incomplete': '没有取得完整微博正文，可以重新尝试。',
    'weibo_snapshot_changed': '微博内容变化，原有待处理来源已保留。',
    'weibo_upstream_failed': '微博内容获取失败，可以重新尝试。',
    'weibo_source_unavailable': '当前会话无法读取这条微博内容，本次已停止。',
    'weibo_browser_unavailable': '微博浏览器连接不可用，请检查 OpenCLI 扩展。',
    'weibo_runtime_unavailable': '微博采集组件未就绪，请检查 OpenCLI 与 Node.js。',

    'x_not_configured': '请先在设置中连接 X。',
    'x_login_required': '需要重新登录 X 后再试。',
    'x_connection_changed': 'X 连接已变化，请重试以绑定当前连接。',
    'x_identity_mismatch': '取得的帖文与提交链接不一致，本次已停止。',
    'x_input_unsupported': '仅支持 X 普通帖文的正文和静态图片，暂不支持视频、GIF 或 Article。',
    'x_snapshot_unknown': '无法确认完整的帖文快照，本次已停止。',
    'x_text_incomplete': '没有取得完整帖文正文，可以重新尝试。',
    'x_media_incomplete': '帖文图片未完整取得，可以重新尝试。',
    'x_media_invalid': '帖文图片未通过完整解码检查，可以重新尝试。',
    'x_snapshot_changed': '帖文内容变化，原有待确认来源已保留。',
    'x_upstream_failed': 'X 内容获取失败，可以重新尝试。',
    'x_browser_unavailable': 'X 浏览器连接不可用，请检查 OpenCLI 扩展。',
    'x_runtime_unavailable': 'X 采集组件未就绪，请检查 OpenCLI 与 Node.js。',

    "source_cleanup_failed": "临时素材未能安全清理，本条暂未继续；已有来源和知识保留。",
    "ocr_runtime_unavailable": "本地图片文字识别组件不可用，请修复应用安装后重试。",
    "ocr_model_unavailable": "图片文字识别模型暂不可用，请检查网络后重试。",
    "ocr_inference_failed": "图片文字识别失败，请重试。",
    "ocr_invalid_output": "图片识别结果或坐标不完整，未建立来源事实。",
    "ocr_invalid_image": "图片无法解码，未建立来源事实。",
    "ocr_legacy_source_requires_review": "该图片来源使用旧版提取方式，已保留原始记录，需要检查后重新采集。",
    "docling_component_missing": "文档模型缺失或校验失败，请使用安装器选择当前数据目录修复，再重试本条任务。",
    "docling_component_corrupt": "文档模型缺失或校验失败，请使用安装器选择当前数据目录修复，再重试本条任务。",
    "docling_component_unsafe_path": "文档模型缺失或校验失败，请使用安装器选择当前数据目录修复，再重试本条任务。",
    "docling_component_unreadable": "文档模型缺失或校验失败，请使用安装器选择当前数据目录修复，再重试本条任务。",
    "docling_component_invalid_inventory": "文档模型缺失或校验失败，请使用安装器选择当前数据目录修复，再重试本条任务。",
    "docling_runtime_unavailable": "本地文档读取组件不可用，请修复 Docling 安装后重试。",
    "docling_conversion_failed": "文档转换暂未完成，原文件已保留，可以重试。",
    "docling_incomplete": "文档转换不完整，未建立来源事实。",
    "docling_invalid_output": "文档结构或定位不完整，未建立来源事实。",
    "pdf_pages_incomplete": "PDF 页数与原件不一致，未建立来源事实。",
    "epub_image_missing": "EPUB 内引用的图片缺失，未建立来源事实。",
    "epub_text_incomplete": "EPUB 章节文字未完整转换，未建立来源事实。",
    "douyin_text_invalid": "抖音图文或长文章的正文、图片不完整，未建立来源事实。",
    "douyin_article_incomplete": "抖音返回的是截断文章，未建立来源事实。",
    "douyin_gallery_incomplete": "抖音图文正文未完整取得，未建立来源事实，可以重试。",
    "douyin_connection_changed": "抖音连接已变更，请确认当前账号后重试。",
    "xiaohongshu_not_configured": "请先在设置中连接小红书。",
    "xiaohongshu_login_required": "需要重新登录小红书后再试。",
    "xiaohongshu_connection_changed": "小红书连接已变化，请重试以绑定当前连接。",
    "xiaohongshu_identity_mismatch": "取得的笔记与提交链接不一致，本次已停止。",
    "xiaohongshu_input_unsupported": "仅支持普通图文和视频笔记，暂不支持长文或集合。",
    "xiaohongshu_text_incomplete": "没有取得完整笔记正文，可以重新尝试。",
    "xiaohongshu_media_incomplete": "笔记图片或视频未完整取得，可以重新尝试。",
    "xiaohongshu_media_invalid": "笔记媒体未通过完整解码检查，可以重新尝试。",
    "xiaohongshu_snapshot_changed": "笔记内容变化，原有待确认来源已保留。",
    "xiaohongshu_upstream_failed": "小红书内容获取失败，可以重新尝试。",
    "xiaohongshu_browser_unavailable": "小红书浏览器连接不可用，请检查 OpenCLI 扩展。",
    "xiaohongshu_runtime_unavailable": "小红书采集组件未就绪，请检查 OpenCLI 与 Node.js。",
    "source_copy_unavailable": "原文件副本丢失或已修改，请重新上传相同原文件补回；已有修改请先另存并移走。",
    "source_snapshot_unavailable": "原提交副本不可用，本次没有建立来源事实。",
    "direct_snapshot_mismatch": "文字或来源声明与原提交不一致，本次没有建立来源事实。",
    "file_snapshot_mismatch": "文件与原提交不一致，本次没有建立来源事实。",
    "pdf_ocr_required": "PDF 包含需要图片识别的内容，当前不能完整读取，未建立来源事实。",
    "pdf_visual_content_uncertain": "PDF 的图形内容无法可靠还原，未建立来源事实。",
    "pdf_reading_order_uncertain": "PDF 的阅读顺序无法可靠确定，未建立来源事实。",
    "pdf_metadata_conflict": "PDF 的作者声明冲突，请核对文件后重新投递。",
    "epub_chapter_missing": "EPUB 缺少阅读顺序中的章节，未建立部分来源事实。",
    "epub_footnote_missing": "EPUB 的注释引用不完整，未建立来源事实。",
    "epub_metadata_conflict": "EPUB 的来源身份声明冲突，请核对后重新投递。",
    "epub_visual_only": "EPUB 包含无法可靠还原的图片正文，未建立来源事实。",
    "processing_unexpected_failure": "处理意外中断，已保留成立的结果，请重试。",
    "youtube_not_configured": "请先在设置中连接 YouTube。",
    "youtube_login_required": "需要重新登录 YouTube 后再试。",
    "youtube_connection_changed": "YouTube 连接在本次处理期间发生变化，请重新投递。",
    "youtube_input_unsupported": "仅支持已结束且内容完整的 YouTube 视频或 Shorts。",
    "youtube_snapshot_changed": "同一视频的音频内容发生了变化，原有待处理来源已保留，本次停止。",
    "youtube_identity_mismatch": "取得的视频与提交链接不一致，本次已停止。",
    "youtube_upstream_failed": "YouTube 内容获取失败，可以重新尝试。",
    "youtube_media_invalid": "没有取得完整 YouTube 音频，本次已停止。",
    "youtube_caption_incomplete": "本次字幕未完整取得，可以重新尝试。",
    "youtube_runtime_unavailable": "YouTube 采集组件未就绪，请检查 Node.js 与 yt-dlp。",
    "douyin_login_required": "需要重新登录抖音后再试。",
    "chrome_connection_failed": "暂时无法连接已打开的 Chrome。",
    "chrome_remote_debugging_disabled": "请先在设置中连接 Chrome。",
    "douyin_not_configured": "请先在设置中连接抖音。",
    "douyin_input_unsupported": "该抖音作品类型尚不支持；可提交普通视频、图集或长文章。",
    "douyin_browser_missing": "未找到 Chrome，请安装后在设置中连接抖音。",
    "douyin_browser_unavailable": "抖音专用浏览器未能启动，请在设置中重新连接。",
    "douyin_source_unavailable": "没有取得完整来源内容，本次已经停止。",
    "douyin_upstream_failed": "抖音来源本次获取失败，可以重新尝试。",
    "douyin_media_invalid": "取得的来源媒体不完整，本次没有继续。",
    "llm_not_configured": "请先在设置中配置语言模型。",
    "llm_secret_unavailable": "无法读取模型密钥，请重新配置。",
    "llm_config_unavailable": "当前语言模型配置不可用，请重新配置。",
    "llm_request_failed": "语言模型本次调用失败，可以重新尝试。",
    "review_checkpoint_unavailable": "来源整理检查点无法保存，请检查可用空间和目录权限后重试。",
    "asr_configuration_changed": "语音识别配置已变化；旧任务仍需完成临时资源清理，请恢复原配置后重试。",
    "asr_checkpoint_unavailable": "转写检查点无法保存，请检查可用空间和目录权限后重试。",
    "review_request_timeout": "来源整理响应超时，已保留可恢复进度，重试会接续处理。",
    "ocr_review_failed": "图片疑点分析未完成，原图与原始文字已保留，可以重试。",
    "ocr_review_invalid": "图片疑点分析结果不完整，未改写来源，可以重试。",
    "llm_request_timeout": "语言模型响应超时，未保存不完整结果，可以重试。",
    "llm_fast_unavailable": "此型号暂不支持 Fast，请在设置中更新型号或选择标准速度。",
    "knowledge_not_qualified": "本次未生成知识，旧记录未保存具体原因。来源已保留，可重新提炼查看新的判断。",
    "knowledge_json_invalid": "模型结果未形成可靠知识，可以重新尝试。",
    "knowledge_structure_invalid": "模型结果未通过证据校验，可以重新尝试。",
    "knowledge_evidence_invalid": "生成的证据引用了尚未确认的图片文字或无效图片依据，本次未保存知识。",
    "review_runtime_unavailable": "请先在设置中完成语言模型配置。",
    "review_runtime_failed": "来源整理本次失败，可以重新尝试。",
    "review_incomplete": "来源整理没有完整结束，可以重新尝试。",
    "review_invalid_output": "来源没有形成可靠文本，可以重新尝试。",
    "review_insufficient_coverage": "整理文本未完整覆盖来源，本次没有继续。",
    "asr_runtime_unavailable": "当前语音识别方案不可用，请在设置中检查已选方案的凭据、权限或运行组件。",
    "asr_runtime_failed": "语音识别本次失败，可以重新尝试。",
    "asr_incomplete": "语音没有被完整恢复，本次没有继续。",
    "asr_empty_output": "语音中没有恢复出可用内容。",
    "audio_conversion_failed": "来源音频本次转换失败，可以重新尝试。",
    "audio_output_invalid": "来源音频不完整，本次没有继续。",
    "confirmation_audio_unavailable": "局部原音准备失败，已保留素材，可以重试。",
    "xiaohongshu_security_restricted": "小红书限制了当前网络访问，请切换可靠网络后重试。",
    "vault_not_configured": "知识已经整理完成，请选择 Obsidian Vault 后重试收录。",
    "Obsidian Vault is unavailable": "原 Obsidian Vault 位置不可用，请修改后重试。",
    "obsidian_target_conflict": "目标位置已有不同内容，为保护原文件没有覆盖。",
    "markdown_invalid_utf8": "文件不是完整的 UTF-8 Markdown，请修正编码后重新投递。",
    "markdown_invalid_bom": "文件包含不受支持的编码标记，请修正后重新投递。",
    "markdown_frontmatter_invalid": "文件的来源声明格式无法可靠读取，请修正后重新投递。",
    "markdown_metadata_conflict": "文件的作者或来源声明冲突，请核对后重新投递。",
    "markdown_unsupported_syntax": "文件包含无法可靠读取的动态或隐藏内容，本次没有建立来源事实。",
    "markdown_empty_content": "文件没有可读取的正文，请补充后重新投递。",
    "source_unconfirmed": "尚有来源疑点未确认，已保留判断进度，可返回继续。",
}


def create_app(
    store: Store,
    distiller: Distiller | Callable[[], Distiller],
    settings_service: SettingsService | None = None,
    *,
    wake_worker: Callable[[], None] | None = None,
    organization=None,
    collection_service=None,
) -> Flask:
    app = Flask(__name__, template_folder="templates", static_folder="static")
    store.initialize()
    from .update_web import register_updates
    register_updates(app, store.path.parent)
    settings_service = settings_service or SettingsService(store)
    # Starting the app must not mutate Keychain items. Label updates belong to
    # explicit configuration saves; a new app signature may require permission.
    app.register_blueprint(settings_blueprint(settings_service))
    from .collections import Collections, CollectionDiscovery
    from .douyin_collections import DouyinCollections, CollectionError
    from .collection_web import collection_blueprint, MESSAGES
    from .settings import AuthorizedDouyinSession
    collection_service = collection_service or Collections(store,CollectionDiscovery(DouyinCollections(store,AuthorizedDouyinSession(store,settings_service.chrome))))
    app.extensions['collections'] = collection_service
    ERROR_TEXT.update(MESSAGES)
    app.register_blueprint(collection_blueprint(store,collection_service,wake_worker,ERROR_TEXT))

    from .insight_web import insight_blueprint
    app.register_blueprint(insight_blueprint(store, _obsidian_url, _publication_file))

    from .topic_web import topic_blueprint
    app.register_blueprint(topic_blueprint(store, _obsidian_url))

    @app.context_processor
    def organization_context():
        from .organization import organization_status
        try:
            return {'organization_status': organization_status(store)}
        except (ValueError, sqlite3.Error):
            return {'organization_status': {'pending': 0, 'error': '暂时无法读取待整理知识。'}}

    @app.post('/organization')
    def organize():
        from knowledge_distiller.organization_service import OrganizationStartKind
        if organization is None:
            return '整理服务尚未启动，请从日常启动入口打开程序。', 503, {'Content-Type': 'text/plain; charset=utf-8'}
        engine = organization() if callable(organization) else organization
        result = engine.start_or_reuse()
        if result.kind is OrganizationStartKind.READ_FAILED:
            return '暂时无法读取待整理知识，本次没有开始。', 503, {'Content-Type': 'text/plain; charset=utf-8'}
        if wake_worker is not None:
            wake_worker()
        return redirect(url_for('home'))

    def service() -> Distiller:
        return distiller() if callable(distiller) else distiller

    @app.get("/")
    def home():
        store.expire_submitted_sources()
        selected = request.args.get("item", type=int)
        return render_template("home.html", **_home_context(store, selected))

    from .link_intake import LinkIntake
    link_intake = LinkIntake(store, settings_service, collection_service, ERROR_TEXT)
    app.extensions['link_intake'] = link_intake
    submit_link = link_intake.submit

    @app.post("/submissions")
    def submit():
        value = request.form.get("content", "")
        try:
            files = [file for file in request.files.getlist("attachment") if file.filename]
            if files:
                if len(files) != 1 or value.strip():
                    raise ValueError("请一次提交一段文本或一个文件，暂不混合投递。")
                item_id = store.submit_source(prepare_file(files[0].filename, files[0].read()))
            elif URL_RE.search(value) and request.form.get('content_kind') != 'text':
                if request.form.get('content_kind') != 'links' and needs_content_choice(value):
                    return render_template('home.html', input_choice=True, **_home_context(
                        store, None, draft=value)), 200
                links = links_in(value)
                mode = request.form.get('processing_mode') or ('separate' if len(links)==1 else None)
                if mode is None:
                    return render_template('home.html', processing_choice=True, **_home_context(store, None, draft=value))
                if mode not in {'separate', 'same_topic'}:
                    raise ValueError('请选择分别蒸馏或同题处理。')
                if mode == 'same_topic':
                    if len(links) < 2 or len({platform_for_url(link) for link in links}) != 1 or platform_for_url(links[0]) is None:
                        raise ValueError('同题处理需要至少两条同一支持平台的素材链接，暂时不能跨平台。')
                    preview = collection_service.preview_same_topic(links, submitted_text=value)
                    return redirect(url_for('collections.preview', token=preview['token']))
                if len(links) == 1:
                    result = submit_link(value)
                    if isinstance(result, dict):
                        return redirect(url_for('collections.preview', token=result['token']))
                    item_id = result
                else:
                    accepted, rejected = [], []
                    for link in links:
                        try:
                            result = submit_link(link)
                            if isinstance(result, dict):
                                collection_service.dismiss(result['token'])
                                raise ValueError('这个链接需要确认范围，请单独投递。')
                            accepted.append(result)
                        except (ValueError, ChromeSessionError, CollectionError) as error:
                            rejected.append((link, ERROR_TEXT.get(str(error), str(error))))
                    if rejected:
                        if accepted and wake_worker is not None:
                            wake_worker()
                        detail = '；'.join(f'{link}：{error}' for link, error in rejected)
                        return render_template('home.html', **_home_context(
                            store, None, form_error=f'已接收 {len(accepted)} 条。未接收：{detail}',
                            draft='\n'.join(link for link, _ in rejected))), 400
                    item_id = accepted[-1]
            else:
                declarations = {key: request.form[key] for key in ("author", "origin", "original_title") if request.form.get(key)}
                item_id = store.submit_source(prepare_direct_text(value, declarations))
        except (ValueError, ChromeSessionError, CollectionError) as error:
            return render_template(
                "home.html",
                input_choice=False,
                **_home_context(store, None, form_error=ERROR_TEXT.get(str(error), str(error)), draft=value),
            ), 400
        if wake_worker is not None:
            wake_worker()
        return redirect(url_for("home", item=item_id))

    @app.post("/items/<int:item_id>/open-source-file")
    def open_source_file(item_id: int):
        try:
            store.open_source_file(item_id)
        except ValueError as error:
            return str(error), 400
        return redirect(url_for("home", item=item_id))

    @app.post('/items/<int:item_id>/open-publication/<action>')
    def open_publication(item_id, action):
        row = store.item_bundle(item_id)
        if row is None or action not in {'file', 'folder'} or not row['published_path'] or not row['published_vault']:
            abort(404)
        try:
            open_saved_location(row['published_vault'], row['published_path'], reveal=action == 'file')
        except ValueError as error:
            return render_template('home.html', **_home_context(store, None, form_error=str(error))), 400
        return redirect(url_for('home', item=item_id))

    @app.get('/items/<int:item_id>/confirmation-image/<concern_id>')
    def confirmation_image(item_id, concern_id):
        row = store.item_bundle(item_id)
        if row is None or row['state'] != 'waiting_user' or not row['confirmation_json']:
            abort(404)
        pending = json.loads(row['confirmation_json'])
        if pending.get('kind') != 'image' or pending.get('token') != request.args.get('token'):
            abort(404)
        concern = next((c for c in pending['concerns'] if c['audio_name'] == concern_id), None)
        if concern is None:
            abort(404)
        member = next((m for m in store.media_members(row['material_id']) if m['member_id'] == concern['member_id']), None)
        if member is None:
            abort(404)
        from .image_confirmation import crop_original
        response = send_file(crop_original(member, concern), mimetype='image/png', max_age=0)
        response.headers['Cache-Control'] = 'no-store'
        return response

    @app.post("/items/<int:item_id>/confirm")
    def confirm(item_id: int):
        action = request.form.get("action", "")
        value = request.form.get("value", "")
        try:
            submitted_value = value
            if action == "manual" and value.strip() and request.form.get("local_edit") == "1" and "local_prefix" in request.form:
                submitted_value=request.form["local_prefix"]+value.strip()+request.form.get("local_suffix", "")
            elif action == "manual" and value.strip() and request.form.get("local_edit") == "1":
                row = store.item_bundle(item_id)
                pending = json.loads(row["confirmation_json"]) if row and row["confirmation_json"] else {}
                if not english_assistance(pending.get("snapshot", "")):
                    concern = next((c for c in pending.get("concerns", []) if c.get("audio_name") == request.form.get("concern_id")), None)
                    if concern:
                        display = local_choices(concern)
                        submitted_value = display['prefix'] + value.strip() + display['suffix']
            result = service().resolve(
                item_id, action, submitted_value, token=request.form.get("token", ""),
                concern_id=request.form.get("concern_id", ""),
                **({"concern_revision": request.form["concern_revision"]} if request.form.get("concern_revision") else {}),
            )
        except ValueError as error:
            return render_template(
                "home.html",
                **_home_context(store, item_id, confirmation_error={
                    "id": item_id, "concern_id": request.form.get("concern_id", ""),
                    "message": str(error), "value": value,
                }),
            ), 400
        if result.state == "queued" and wake_worker is not None:
            wake_worker()
        return redirect(url_for("home", item=item_id))

    @app.get("/items/<int:item_id>/confirmation-audio")
    def confirmation_audio(item_id: int):
        path = service().confirmation_audio(item_id, request.args.get("concern_id", ""))
        if path is None:
            abort(404)
        response = send_file(path, mimetype="audio/wav", conditional=True)
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.post('/items/<int:item_id>/recover-confirmation-audio')
    def recover_confirmation_audio(item_id: int):
        try:
            service().recover_confirmation_audio(item_id, token=request.form.get('token', ''),
                                                 concern_id=request.form.get('concern_id', ''))
        except (ValueError, LookupError) as error:
            return str(error), 409
        return redirect(url_for('home', item=item_id))

    @app.post("/items/<int:item_id>/continue")
    def continue_knowledge(item_id: int):
        try:
            result = service().finish_transcript(item_id, token=request.form.get("token", ""))
        except (ValueError, LookupError) as error:
            return str(error), 400
        if result.state == "queued" and wake_worker is not None:
            wake_worker()
        return redirect(url_for("home", item=item_id))

    @app.post("/items/<int:item_id>/suggestions")
    def suggest_candidates(item_id: int):
        from .llm import LLMRequestError
        try:
            service().suggest_candidates(item_id, token=request.form.get('token', ''),
                concern_id=request.form.get('concern_id', ''))
        except (ValueError, LookupError) as error:
            return str(error), 409
        except LLMRequestError:
            return '候选暂时未能生成，已有待办保留，请稍后再试。', 503
        return redirect(url_for('home', item=item_id))

    @app.post("/items/<int:item_id>/rerecognize")
    def rerecognize(item_id: int):
        try:
            result = service().rerecognize(item_id, token=request.form.get("token", ""))
        except (LookupError, ValueError):
            return "Only waiting items can be recognized again", 409
        if result.state == "queued" and wake_worker is not None:
            wake_worker()
        return redirect(url_for("home", item=item_id))

    @app.post("/items/<int:item_id>/dismiss")
    def dismiss(item_id: int):
        try:
            store.dismiss_item(item_id)
        except ValueError as error:
            return str(error), 409
        return redirect(url_for('home'))

    @app.post("/items/<int:item_id>/retry")
    def retry(item_id: int):
        row = store.item_bundle(item_id)
        if row is None:
            return "Not found", 404
        if row["error_code"] == "source_unconfirmed" and (
            row["confirmation_json"] or request.form.get("action") != "rerecognize"
        ):
            return "请返回来源确认，重新识别需明确发起。", 409
        try:
            replacement = None
            if request.files.get("attachment"):
                file = request.files["attachment"]
                replacement = prepare_file(file.filename, file.read())
            elif request.form.get("content"):
                declarations = {key: request.form[key] for key in ("author", "origin", "original_title") if request.form.get(key)}
                replacement = prepare_direct_text(request.form["content"], declarations)
            store.retry_item(item_id, replacement)
        except ValueError as error:
            return str(error), 409
        if wake_worker is not None:
            wake_worker()
        return redirect(url_for("home", item=item_id))

    @app.post("/items/<int:item_id>/resume-confirmation")
    def resume_confirmation(item_id: int):
        try:
            store.return_to_confirmation(item_id)
        except ValueError as error:
            return str(error), 409
        return redirect(url_for("home", item=item_id))

    return app


from .link_intake import douyin_url


def _home_context(
    store: Store,
    selected: int | None,
    *,
    form_error: str | None = None,
    draft: str = "",
    confirmation_error: dict | None = None,
) -> dict[str, object]:
    rows = list(store.recent_items())
    vault_path = store.setting("vault_path")
    selected_row = store.item_bundle(selected) if selected is not None else None
    # Repeated submissions point to the same knowledge card, including deep links.
    if selected_row is not None and selected_row["state"] == "succeeded" and selected_row["knowledge_result_id"] is not None:
        selected_row = next((row for row in rows
            if row["state"] == "succeeded" and row["knowledge_result_id"] == selected_row["knowledge_result_id"]), selected_row)
        selected = selected_row["item_id"]
    if selected_row is not None and selected_row['dismissed_at'] is None and not any(r['item_id']==selected for r in rows):
        rows.insert(0,selected_row)
    from .collections import Collections
    from .collection_web import cards
    collection_cards = cards(Collections(store))
    selected_collection = next((c for c in collection_cards if any(m['item_id']==selected for m in c['members'])),None)
    processing = tuple(
        _item_view(row, vault_path, store.path.parent) for row in rows if row["state"] == "working"
    )
    waiting = tuple(
        _item_view(row, vault_path, store.path.parent) for row in sorted((r for r in rows if r["state"] == "queued"),
            key=lambda r: (r["queued_at"] or "", r["item_id"]))
    )
    todo = tuple(
        _item_view(row, vault_path, store.path.parent)
        for row in rows
        if row["state"] in {"waiting_user", "failed"}
    )
    recent = tuple(
        _item_view(row, vault_path, store.path.parent)
        for row in rows
        if row["state"] == "succeeded" and row["payload_json"] is not None
    )
    return {
        "collection_cards": collection_cards,
        "selected_collection": selected_collection,
        "collection_processing": sum(c['state']=='working' for c in collection_cards),
        "collection_waiting": sum(c['counts']['queued'] for c in collection_cards if c['state']=='queued'),
        "selected": (
            _item_view(selected_row, vault_path, store.path.parent)
            if selected_row is not None
            else None
        ),
        "processing": processing,
        "waiting": waiting,
        "todo": todo,
        "recent": recent,
        "form_error": form_error,
        "draft": draft,
        "confirmation_error": confirmation_error,
        "confirmation_count": sum(max(1, len(item["confirmation"]["concerns"])) for item in todo if item["state"] == "waiting_user"),
    }


def _item_view(row, vault_path: str | None, data_root=None) -> dict[str, object]:
    payload = json.loads(row["payload_json"]) if row["payload_json"] else {}
    metadata = json.loads(row["metadata_json"]) if row["metadata_json"] else {}
    confirmation = (
        json.loads(row["confirmation_json"]) if row["confirmation_json"] else None
    )
    if confirmation is not None:
        from .confirmation_revision import revision
        for concern in confirmation.get("concerns", []):
            concern["revision"] = revision(confirmation, concern)
        confirmation.setdefault("review_required", True)
        text = confirmation.get("snapshot", "")
        confirmation["english_assistance"] = english_assistance(text)
        if not confirmation["english_assistance"]:
            for concern in confirmation.get("concerns", []):
                concern["display"] = local_choices(concern)
    author = metadata.get("author", {}) if isinstance(metadata, dict) else {}
    kind = row["source_kind"] or row["input_kind"] or platform_for_url(row["submitted_url"]) or "unknown"
    local = kind in {"direct_text", "markdown", "pdf", "epub"}
    from .source_files import FILE_KINDS, copy_path, SourceCopyError
    available = False
    if kind in FILE_KINDS and data_root is not None:
        try:
            available = copy_path(data_root, kind, row['input_key'], row['input_label']).is_file()
        except SourceCopyError:
            pass
    publication = publication_status(row["published_vault"], row["published_path"])
    return {
        "file_source": kind in FILE_KINDS,
        "source_copy_available": available,
        "local_source": local,
        "retryable": row["retryable"] != 0,
        "needs_original": local and row["retryable"] != 0 and (not available if kind in FILE_KINDS else not row["input_available"]) and row["material_id"] is None,
        "input_kind": kind,
        "id": row["item_id"],
        "state": row["state"],
        "phase": row["phase"],
        "title": payload.get("title") or metadata.get("source_title") or metadata.get("original_description") or row["input_label"] or row["submitted_title"] or LABELS.get(kind, "未知来源") + (" 内容" if kind in {"x", "youtube"} else "内容"),
        "subtitle": payload.get("subtitle") or "",
        "review_notice": _review_notice(row),
        "summary": payload.get("summary") or "",
        "source": "" if local else author.get("display_name") or "",
        "source_scope": metadata.get('source_scope', ''),
        "canonical_url": "" if local else row['submitted_url'] if kind == 'xiaohongshu' else row["canonical_url"] or row["submitted_url"],
        "source_type": LABELS.get(kind, "未知来源"),
        "submitted_at": _submitted_at(row["created_at"]),
        "stages": _stage_states(row["phase"]),
        "collected_at": _collected_at(row["published_at"]),
        "obsidian_url": publication["url"],
        "publication_saved": publication["file"] is not None,
        "publication": publication,
        "core_points": [
            point.get("statement", "") for point in payload.get("core_points", [])
        ],
        "other_points": [
            point.get("statement", "") for point in payload.get("other_points", [])
        ],
        "error": (row['rejection_reason']
                  if row['error_code'] == 'knowledge_not_qualified' and row['rejection_reason']
                  else "旧记录没有可恢复的判断进度，重新识别会重跑素材。"
                  if row["error_code"] == "source_unconfirmed" and not confirmation
                  else ERROR_TEXT.get(row["error_code"], "无法完整读取此文件，请修正内容后重新投递。" if row["retryable"] == 0 else "本次处理未完成，可以稍后重试。")),
        "error_code": row["error_code"],
        "needs_settings": row["error_code"] in {"weibo_not_configured", "weibo_runtime_unavailable", "zhihu_not_configured", "zhihu_runtime_unavailable", "x_not_configured", "x_runtime_unavailable", "xiaohongshu_not_configured", "xiaohongshu_runtime_unavailable", "youtube_not_configured", "youtube_runtime_unavailable", "vault_not_configured", "douyin_not_configured", "chrome_remote_debugging_disabled", "llm_not_configured", "asr_runtime_unavailable", "review_runtime_unavailable"},
        "confirmation": confirmation,
    }


def _collected_at(value: str | None) -> str:
    if value is None:
        return ""
    published = datetime.fromisoformat(value).astimezone()
    return f"收录于  {published.month} 月 {published.day} 日"


def _submitted_at(value: str) -> str:
    submitted = datetime.fromisoformat(value).astimezone()
    return f"{submitted.month} 月 {submitted.day} 日  {submitted:%H:%M}"


def _stage_states(current: str) -> tuple[dict[str, str], ...]:
    phases = (
        ("collecting", "采集"),
        ("reviewing", "整理"),
        ("distilling", "提炼"),
        ("publishing", "收录"),
    )
    current_index = next(
        (index for index, (phase, _) in enumerate(phases) if phase == current), 0
    )
    return tuple(
        {
            "label": label,
            "state": "green" if index < current_index else "yellow" if index == current_index else "grey",
        }
        for index, (_, label) in enumerate(phases)
    )


def _publication_file(vault_path: str | None, published_path: str | None) -> Path | None:
    return publication_status(vault_path, published_path)['file']


def _obsidian_url(vault_path: str | None, published_path: str | None) -> str | None:
    return publication_status(vault_path, published_path)['url']


def _review_notice(row):
    lineage = json.loads(row['lineage_json'] or '{}') if 'lineage_json' in row.keys() else {}
    diagnostics = lineage.get('review_diagnostics', []) + lineage.get('ocr_review_diagnostics', [])
    count = len({(d.get('segment'), d.get('operation')) for d in diagnostics})
    return f'已保留原文继续，{count}项修改或段落未采用。' if count else ''

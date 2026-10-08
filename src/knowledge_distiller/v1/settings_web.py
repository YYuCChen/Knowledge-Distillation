from __future__ import annotations

import re
import sys

from flask import Blueprint, abort, current_app, g, redirect, render_template, request, url_for
from urllib.parse import urlsplit

from .chrome import ChromeSessionError
from .settings import SettingsError, SettingsService


MESSAGES = {
    'local_address_busy': '任务或组件安装正在进行，请完成后再更改地址。原地址保持不变。',
    'local_address_save_failed': '地址未保存成功，请检查应用数据目录后重试。原地址保持不变。',
    'local_address_name_invalid': '名称只能包含小写英文字母、数字和连字符，且不能以连字符开头或结尾。',
    'local_address_port_invalid': '端口必须是 1024 至 65535 之间的整数。',
    'local_address_port_busy': '这个端口正被其他程序占用，地址没有更改。请换一个端口后重试。',
    'local_address_config_invalid': '本地访问地址配置无法读取，请恢复默认地址。',
    'credentials_open_failed': '无法打开密钥文件夹。',
    'reading_style_disabled': '笔记排版已关闭。',

    'doubao_storage_listed': '已读取存储桶。选择用于音频处理的桶，地域会自动填写。尚未验证文件读写权限。',
    'doubao_storage_empty': '当前账号没有可列出的存储桶。创建私有桶后，点击重新读取；密钥已保存。',
    'doubao_storage_credentials_invalid': '存储密钥未通过检查。请复制同一组 Access Key ID 和 Secret Access Key，勿使用语音 API Key。',
    'doubao_storage_list_denied': '当前密钥没有列出存储桶的权限。可以在下方手动填写已获授权的桶，无需扩大权限。',
    'doubao_storage_clock_invalid': '电脑时间与服务器不一致，请启用系统自动时间后重试。',
    'doubao_storage_unavailable': '暂时无法读取存储桶，请检查网络后重试。原配置保留。',
    'doubao_storage_response_invalid': '存储服务返回的信息无法使用，请重试或手动填写。原配置保留。',
    'doubao_storage_selection_expired': '存储配置已更新，请使用最新列表重新选择。',
    'doubao_storage_selection_required': '请选择列表中的存储桶。',

    'feishu_pairing_started': '应用凭据验证通过。请继续第 3 步配置并发布，再发送绑定口令；这还不代表全部配置完成。',
    'feishu_receipt_renewed': '已发送新的回执卡片；原投递与待办保留，旧卡片操作不会覆盖新状态。',
    'feishu_configuration_saved': '飞书凭据验证通过，已重新连接；已有投递和绑定保持不变。',
    'feishu_configuration_failed': '验证未完成：请检查第 2 步的 App ID、App Secret 和机器人能力，以及网络连接，然后重试。已有配置与投递保留。',
    'feishu_binding_required': '请先完成专用机器人的私聊绑定；不能在这里覆盖已有绑定。',
    "reading_style_installed": "笔记排版已启用。",
    "reading_style_conflict": "阅读样式未启用，现有文件或设置已保留。请检查 Vault 中同名样式。",
    "model_credentials_saved": "凭据已保存，尚未更换启用方案。",
    "model_credentials_invalid": "请先完整保存所需凭据。",
    "model_credentials_save_failed": "凭据未保存成功，原配置保持不变。",
    "doubao_runtime_unavailable": "豆包识别组件尚未安装，原配置保持不变。",
    "codex_connected": "本机 Codex 已连接，可选择模型。",
    "codex_connection_failed": "未成功连接。",
    "codex_effort_invalid": "所选型号不支持此推理强度。",
    "codex_fast_unavailable": "此型号暂不支持 Fast，请更新型号或选择标准速度。",
    'zhihu_connected': '知乎连接已更新。',
    'zhihu_cleared': '知乎本地连接已清除；浏览器登录没有改变。',
    'zhihu_login_required': '请先在当前 Chrome 登录知乎，再次连接。',
    'zhihu_browser_unavailable': '知乎浏览器连接不可用，请检查 OpenCLI 扩展。',
    'zhihu_runtime_unavailable': '知乎采集组件未就绪，请检查 OpenCLI 与 Node.js。',
    'zhihu_upstream_failed': '知乎连接检查失败，可以重试。',
    'weibo_connected': '微博连接已更新。',
    'weibo_cleared': '微博本地连接已清除；浏览器登录没有改变。',
    'weibo_login_required': '请先在当前 Chrome 登录微博，再次连接。',
    'weibo_browser_unavailable': '微博浏览器连接不可用，请检查 OpenCLI 扩展。',
    'weibo_runtime_unavailable': '微博采集组件未就绪，请检查 OpenCLI 与 Node.js。',
    'weibo_upstream_failed': '微博连接检查失败，可以重试。',

    'x_connected': 'X 连接已更新。',
    'x_cleared': 'X 本地连接已清除；浏览器登录没有改变。',
    'x_login_required': '请先在当前 Chrome 登录 X，再次连接。',
    'x_browser_unavailable': 'X 浏览器连接不可用，请检查 OpenCLI 扩展。',
    'x_runtime_unavailable': 'X 采集组件未就绪，请检查 OpenCLI 与 Node.js。',
    'x_upstream_failed': 'X 连接检查失败，可以重试。',

    "xiaohongshu_connected": "小红书连接已更新。",
    "xiaohongshu_cleared": "小红书本地连接已清除；浏览器登录没有改变。",
    "xiaohongshu_login_required": "请先在当前 Chrome 登录小红书，再次连接。",
    "xiaohongshu_browser_unavailable": "小红书浏览器连接不可用，请检查 OpenCLI 扩展。",
    "xiaohongshu_runtime_unavailable": "小红书采集组件未就绪，请检查 OpenCLI 与 Node.js。",
    "xiaohongshu_upstream_failed": "小红书连接检查失败，可以重试。",
    "youtube_connected": "YouTube 连接已更新。",
    "youtube_cleared": "YouTube 本地连接已清除；浏览器登录没有改变。",
    "youtube_login_required": "请先在当前 Chrome 会话登录 YouTube，再次连接。",
    "source_files_opened": "",
    "source_files_open_failed": "未能打开原文件副本目录，请检查应用数据目录。",
    "jev_saved": "Jev 已启用：随手记身份判断和整理时的历史召回改由 Jev 完成。",
    "jev_key_invalid": "Jev 没有接受这个 API Key，请检查后重新保存。",
    "jev_unreachable": "暂时连不上 Jev，API Key 未保存，请稍后再试。",
    "douyin_browser_missing": "未找到 Google Chrome，请先安装后连接抖音。",
    "douyin_browser_unavailable": "抖音专用浏览器未能启动，请重新连接。",
    "douyin_connected": "抖音连接已更新。",
    "douyin_cleared": "抖音专用登录态已清除；日常浏览器不受影响。",
    "llm_saved": "LLM 已保存并启用。",
    "qwen_install_started": "Qwen 安装已开始，完成后请点击保存并启用。",
    "qwen_install_failed": "Qwen 安装未能开始，请稍后重试。原配置保持不变。",
    "asr_saved": "ASR 已保存并启用。",
    "vault_saved": "Obsidian Vault 已更新；既有文件没有移动。",
    "chrome_remote_debugging_disabled": (
        "请先在 Chrome 打开 chrome://inspect/#remote-debugging 并启用远程调试，"
        "然后再次连接。"
    ),
    "chrome_connection_failed": "未成功连接 Chrome，请确认允许了本次连接。",
    "chrome_context_unavailable": "Chrome 当前会话无法使用，请关闭多余调试会话后重试。",
    "douyin_login_required": "请点击抖音“配置登录”或“重新登录”，在专用窗口完成登录。",
    "llm_config_invalid": "请填写有效的 HTTPS Base URL 和模型 ID。",
    "llm_save_failed": "LLM 没有保存成功，原配置保持不变。",
    "asr_runtime_unavailable": "Qwen ASR 运行组件尚未安装，原配置保持不变。",
    "vault_not_writable": "这个目录无法写入，原保存位置保持不变。请检查权限或选择可写目录。",
    "vault_open_failed": "暂时无法打开保存文件夹，请检查目录是否仍存在。",
    "vault_invalid": "请选择一个已经存在的文件夹作为 Obsidian 发布目录。",
    "vault_picker_failed": "没有成功打开路径选择器，原位置保持不变。",
}


for _platform, _label in {'douyin': '抖音', 'youtube': 'YouTube', 'xiaohongshu': '小红书', 'x': 'X', 'zhihu': '知乎', 'weibo': '微博'}.items():
    MESSAGES[_platform + '_login_pending'] = f'已打开{_label}普通登录窗口，请手动登录后回到这里点击“完成登录”。'
    MESSAGES[_platform + '_login_required'] = f'请在设置中重新连接{_label}，在专用窗口完成登录。'
    MESSAGES[_platform + '_browser_unavailable'] = f'{_label}专用浏览器未能启动，请重新连接。'
    MESSAGES[_platform + '_browser_missing'] = '未找到 Google Chrome，请安装后重试。'
    MESSAGES[_platform + '_cleared'] = f'{_label}专用登录态已清除，其他平台不受影响。'

MESSAGES.update({
    'zhihu_connected': '知乎已连接。',
    'zhihu_login_required': '请在日常 Chrome 登录知乎，再连接前台 Chrome。',
    'zhihu_browser_unavailable': '知乎前台连接不可用，请保持日常 Chrome 与 OpenCLI 扩展连接，并手动处理调试授权提示。',
    'zhihu_cleared': '知乎连接已清除。',
    'zhihu_source_unavailable': '知乎限制了本次读取，已停止；原连接保留。',
})


for _platform, _label in {'xiaohongshu': '小红书', 'zhihu': '知乎'}.items():
    MESSAGES.update({
        _platform + '_bridge_start_failed': '浏览器连接组件未能启动。请退出并重新打开知识蒸馏器后重试；仍失败请提供应用日志。无需另装 OpenCLI 命令行。',
        _platform + '_bridge_extension_required': '浏览器连接组件已启动，尚未连接 Chrome 扩展。请打开日常 Chrome，启用 OpenCLI Browser Bridge 扩展，点击 Reconnect，然后重试。',
        _platform + '_bridge_profile_required': '检测到多个浏览器个人资料，尚未确定使用哪一个。请暂时只在要使用的 Chrome 个人资料中保持 Browser Bridge 扩展连接，再重试。',
        _platform + '_bridge_profile_disconnected': '原来连接的 Chrome 个人资料已断开。请打开原个人资料，在 Browser Bridge 扩展中点击 Reconnect 后重试。',
        _platform + '_browser_unavailable': f'{_label}前台连接不可用。请保持日常 Chrome 与 Browser Bridge 扩展连接，再重试。',
        _platform + '_login_required': f'请在已连接的日常 Chrome 中登录{_label}，完成网页验证后再重试。',
        _platform + '_cleared': f'{_label}连接已清除，日常 Chrome 登录资料保持不变。',
    })


def settings_blueprint(service: SettingsService) -> Blueprint:
    blueprint = Blueprint("settings", __name__)
    _register_decision_settings(blueprint, service)

    @blueprint.get("/settings")
    def page():
        from .reading_style import state
        message_key = request.args.get("message", "")
        view = service.view()
        if 'local_address' not in view:
            from .local_address import LocalAddress
            address = LocalAddress()
            view['local_address'] = {'name': address.name, 'port': address.port,
                                     'host': address.host, 'url': address.url}
        return render_template(
            "settings.html",
            windows_runtime=sys.platform == "win32",
            settings=view,
            reading_style_state=state(view.get('vault',{}).get('path')),
            feishu=current_app.extensions['feishu'].status() if 'feishu' in current_app.extensions else None,
            return_to=_return_to(request.args.get("return_to", "")),
            message=MESSAGES.get(message_key),
            message_error=message_key not in {
                "",
                "douyin_connected",
                "zhihu_connected",
                "zhihu_cleared",
                "weibo_connected",
                "weibo_cleared",
                "x_connected",
                "x_cleared",
                "xiaohongshu_connected",
                "xiaohongshu_cleared",
                "youtube_connected",
                "youtube_cleared",
                "douyin_cleared",
                "llm_saved",
                "codex_connected",
                "model_credentials_saved", "credential_saved", "credential_cleared",
                "asr_saved",
                "doubao_storage_listed",
                "doubao_storage_empty",
                "qwen_install_started",
                "vault_saved",
                "reading_style_installed", "reading_style_disabled",
                "jev_saved",
                "feishu_configuration_saved",
                "feishu_receipt_renewed",
                "feishu_pairing_started",
                "source_files_opened",
            },
            open_group=request.args.get("open", ""),
            draft_asr=request.args.get("asr", ""),
            draft_llm=request.args.get("llm", ""),
        )

    @blueprint.post('/settings/jev')
    def save_jev():
        try:
            service.save_jev_key(request.form.get('api_key', ''))
        except SettingsError as error:
            return _back(str(error), 'models')
        return _back('jev_saved', 'models')

    @blueprint.post('/settings/feishu')
    def configure_feishu():
        connection=current_app.extensions.get('feishu')
        if connection is None:return _back('feishu_binding_required','paths')
        try:
            connection.configure(request.form.get('app_id','').strip(),request.form.get('app_secret',''))
        except Exception as error:
            code='feishu_binding_required' if isinstance(error,ValueError) and str(error)=='feishu_binding_required' else 'feishu_configuration_failed'
            return _back(code,'paths')
        return _back('feishu_configuration_saved' if connection.status().get('binding') else 'feishu_pairing_started','paths')

    def apply_local_address(address):
        from .local_address import LocalAddressError, load, port_available, save
        try:
            current_port = current_app.config.get('LOCAL_ADDRESS_PORT', load(service.store.path.parent).port)
            if address.port != current_port and not port_available(address.port):
                raise LocalAddressError('local_address_port_busy')
            restart = current_app.config.get('KNOWLEDGE_DISTILLER_RESTART')
            if restart is None:
                save(service.store.path.parent, address)
                return _back('', 'paths')
            restart(address)
        except LocalAddressError as error:
            return _back(str(error), 'paths')
        except OSError:
            return _back('local_address_save_failed', 'paths')
        return render_template('local_address_restart.html', target_url=address.url)

    @blueprint.post('/settings/local-address')
    def save_local_address():
        from .local_address import LocalAddressError, validate
        try:
            address = validate(request.form.get('name', ''), request.form.get('port', ''))
        except LocalAddressError as error:
            return _back(str(error), 'paths')
        return apply_local_address(address)

    @blueprint.post('/settings/local-address/default')
    def reset_local_address():
        from .local_address import LocalAddress
        return apply_local_address(LocalAddress())

    @blueprint.get('/settings/feishu/status')
    def feishu_status():
        connection=current_app.extensions.get('feishu')
        return connection.status() if connection else {'state':'unbound','binding':{},'pairing':{}}

    @blueprint.post('/settings/feishu/receipt')
    def renew_feishu_receipt():
        connection=current_app.extensions.get('feishu')
        if connection is None:return _back('feishu_binding_required','paths')
        try:connection.renew_receipt(request.form.get('message_id',''),request.form.get('card_id',''))
        except Exception:return _back('feishu_configuration_failed','paths')
        return _back('feishu_receipt_renewed','paths')

    @blueprint.get('/settings/platforms/<platform>/health')
    def platform_health(platform):
        try:
            return service.platform_health(platform)
        except LookupError:
            abort(404)

    @blueprint.post('/settings/<platform>/cancel-login')
    def cancel_login(platform):
        service.cancel_platform_login(platform)
        return _back('', 'social')

    @blueprint.post("/settings/douyin/connect")
    def connect_douyin():
        try:
            service.connect_douyin()
        except ChromeSessionError as error:
            return _back(error.args[0] if error.args else "chrome_connection_failed", "social")
        return _back("douyin_connected", "social")

    @blueprint.post("/settings/douyin/clear")
    def clear_douyin():
        service.clear_douyin()
        return _back("douyin_cleared", "social")

    @blueprint.post('/settings/zhihu/connect')
    def connect_zhihu():
        try:
            service.connect_zhihu()
        except ChromeSessionError as error:
            return _back(str(error), 'social')
        return _back('zhihu_connected', 'social')

    @blueprint.post('/settings/zhihu/clear')
    def clear_zhihu():
        service.clear_zhihu()
        return _back('zhihu_cleared', 'social')

    @blueprint.post('/settings/weibo/connect')
    def connect_weibo():
        try:
            service.connect_weibo()
        except ChromeSessionError as error:
            return _back(str(error), 'social')
        return _back('weibo_connected', 'social')

    @blueprint.post('/settings/weibo/clear')
    def clear_weibo():
        service.clear_weibo()
        return _back('weibo_cleared', 'social')

    @blueprint.post('/settings/x/connect')
    def connect_x():
        try:
            service.connect_x()
        except ChromeSessionError as error:
            return _back(str(error), 'social')
        return _back('x_connected', 'social')

    @blueprint.post('/settings/x/clear')
    def clear_x():
        service.clear_x()
        return _back('x_cleared', 'social')

    @blueprint.post("/settings/xiaohongshu/connect")
    def connect_xiaohongshu():
        from .xiaohongshu import XiaohongshuSourceError
        try:
            service.connect_xiaohongshu()
        except (ChromeSessionError, XiaohongshuSourceError) as error:
            return _back(str(error), 'social')
        return _back('xiaohongshu_connected', 'social')

    @blueprint.post("/settings/xiaohongshu/clear")
    def clear_xiaohongshu():
        service.clear_xiaohongshu()
        return _back('xiaohongshu_cleared', 'social')

    @blueprint.post("/settings/youtube/connect")
    def connect_youtube():
        try:
            service.connect_youtube()
        except ChromeSessionError as error:
            return _back(error.args[0] if error.args else "chrome_connection_failed", "social")
        return _back("youtube_connected", "social")

    @blueprint.post("/settings/youtube/clear")
    def clear_youtube():
        service.clear_youtube()
        return _back("youtube_cleared", "social")

    @blueprint.post("/settings/llm")
    def save_llm():
        try:
            if request.form.get("provider") == "codex":
                service.activate_codex(request.form.get("model", ""), request.form.get("effort", ""),
                                       request.form.get("service_tier", ""))
            elif not request.form.get("api_key"):
                service.activate_saved_api(request.form.get("base_url", ""), request.form.get("model", ""))
            else:
                service.activate_llm(
                    request.form.get("base_url", ""), request.form.get("model", ""),
                    request.form.get("api_key", ""))
        except SettingsError as error:
            return _back(error.args[0] if error.args else "llm_save_failed", "models")
        return _back("llm_saved", "models")

    @blueprint.post("/settings/codex/connect")
    def connect_codex():
        try:
            service.refresh_codex()
        except SettingsError as error:
            return _back(str(error), "models")
        return _back("codex_connected", "models")

    @blueprint.post("/settings/llm/key")
    def save_llm_key():
        try:
            service.save_llm_key(request.form.get("api_key", ""))
        except SettingsError as error:
            return _back(str(error), "models")
        return _back("model_credentials_saved", "models")

    @blueprint.post("/settings/asr/key")
    def save_asr_key():
        try:
            service.save_doubao_key(request.form.get("api_key", ""))
        except SettingsError as error:
            return _back(str(error), "models")
        return _back("model_credentials_saved", "models")

    @blueprint.post("/settings/asr/tos")
    def save_asr_tos():
        try:
            service.save_doubao_tos(*(request.form.get(key, "") for key in
                                     ("region", "bucket", "access_key", "secret_key")))
        except SettingsError as error:
            return _back(str(error), "models")
        return _back("model_credentials_saved", "models")

    @blueprint.post('/settings/asr/storage/discover')
    def discover_asr_storage():
        from .doubao_setup import save_discovery, SetupError
        try:
            found=save_discovery(service,request.form.get('access_key',''),request.form.get('secret_key',''))
        except (SetupError, SettingsError) as error:
            if request.accept_mimetypes.best == 'application/json':
                return {'error':MESSAGES.get(str(error), '读取失败，请重试。')}, 400
            return _back(str(error), 'models')
        result=_back('doubao_storage_listed' if found else 'doubao_storage_empty','models')
        if request.accept_mimetypes.best == 'application/json':
            return {'redirect':result.location}
        return result

    @blueprint.post('/settings/asr/storage/select')
    def select_asr_storage():
        from .doubao_setup import select_bucket, SetupError
        try:
            select_bucket(service.store,request.form.get('discovery_id',''),request.form.get('bucket',''))
        except SetupError as error:
            return _back(str(error),'models')
        return _back('model_credentials_saved','models')

    @blueprint.post("/settings/asr/install")
    def install_qwen():
        from .qwen_component import ComponentError
        try:
            service.qwen_component.start()
        except (ComponentError, OSError):
            return _back("qwen_install_failed", "models")
        return _back("qwen_install_started", "models")

    @blueprint.get("/settings/asr/component")
    def qwen_component_status():
        return service.qwen_component.status()

    @blueprint.post("/settings/asr")
    def save_asr():
        try:
            if request.form.get("provider") == "doubao":
                service.activate_doubao()
            else:
                service.activate_asr()
        except SettingsError as error:
            return _back(error.args[0] if error.args else "asr_runtime_unavailable", "models")
        return _back("asr_saved", "models")

    @blueprint.post("/settings/vault")
    def save_vault():
        try:
            changed = service.choose_vault()
        except SettingsError as error:
            return _back(error.args[0] if error.args else "vault_picker_failed", "paths")
        return _back("vault_saved", "paths") if changed else _back("", "paths")

    @blueprint.post('/settings/vault/open')
    def open_vault():
        from .vault_access import open_saved_location
        try:
            open_saved_location(service.store.setting('vault_path'))
        except (ValueError, OSError):
            return _back('vault_open_failed', 'paths')
        return _back('', 'paths')

    @blueprint.post('/settings/credentials/open')
    def open_credentials():
        from .vault_access import open_saved_location
        from .local_secrets import SecretError
        try:
            service.local_secrets._directory()
            open_saved_location(str(service.local_secrets.root))
        except (ValueError, OSError, SecretError):
            return _back('credentials_open_failed', 'paths')
        return _back('', 'paths')

    @blueprint.post("/settings/source-files/open")
    def open_source_files():
        try:
            service.open_source_files()
        except SettingsError:
            return _back("source_files_open_failed", "paths")
        return _back("", "paths")

    @blueprint.post('/settings/reading-style')
    def reading_style():
        from .reading_style import install, disable
        action = request.form.get('action', 'enable')
        if action not in ('enable','disable'): return _back('reading_style_conflict','paths')
        try:(disable if action == 'disable' else install)(service.store.settings().get('vault_path',''))
        except (ValueError,OSError):return _back('reading_style_conflict','paths')
        return _back('reading_style_disabled' if action == 'disable' else 'reading_style_installed','paths')

    return blueprint


def _back(message: str, group: str):
    if request.path in {'/settings/llm/key', '/settings/asr/key', '/settings/asr/tos'} or request.path.startswith('/settings/asr/storage/'):
        group = 'models'
    values = {"open": group}
    if request.path == '/settings/feishu':
        app_id=request.form.get('app_id','').strip()
        if re.fullmatch(r'cli_[a-zA-Z0-9]{1,100}',app_id):
            values['feishu_app_id']=app_id
    if request.path in {"/settings/asr/key", "/settings/asr/tos"}:
        values["asr"] = "doubao"
        saved = message == 'model_credentials_saved'
        values['asr_step'] = (3 if saved else 2) if request.path.endswith('/key') else (4 if saved else 3)
        if request.path.endswith('/tos'):
            if not saved:values['asr_manual']='1'
            region=request.form.get('region','').strip()
            bucket=request.form.get('bucket','').strip()
            if re.fullmatch(r'[a-z]{2}-[a-z]+(?:-\d+)?',region):values['asr_region']=region
            if re.fullmatch(r'[a-z0-9][a-z0-9-]{1,61}[a-z0-9]',bucket):values['asr_bucket']=bucket
    if request.path == '/settings/asr' and request.form.get('provider') == 'doubao':
        values.update(asr='doubao',asr_step=4)
    if request.path.startswith('/settings/asr/storage/'):
        values['asr']='doubao'
        values['asr_step']=4 if request.path.endswith('/select') and message == 'model_credentials_saved' else 3
    if request.path == "/settings/asr/install":
        values["asr"] = "qwen"
    if request.path == "/settings/llm/key":
        values["llm"] = "openai"
    return_to = _return_to(request.form.get("return_to", ""))
    if return_to != "/":
        values["return_to"] = return_to
    if message:
        values["message"] = message
    return redirect(url_for("settings.page", **values))


def _return_to(value):
    return value if re.fullmatch(r"/(?:topics(?:/[0-9]+)?|insights)(?:\?[^\r\n\\]*)?", value) else "/"

def _register_decision_settings(blueprint, service):
    """U06: isolated form receipts, exact checked fields and activation CAS."""
    from collections import OrderedDict
    from copy import copy
    import secrets
    import threading
    import time
    from .settings import SettingsError
    from .decision_client import JEV_ENDPOINT

    rows, lock = OrderedDict(), threading.RLock()
    cookie = 'kd-decision-form'

    def current():
        token = request.cookies.get(cookie, '')
        now = time.monotonic()
        for old in list(rows):
            if now - rows[old]['touched'] > 3600:
                del rows[old]
        if token not in rows:
            if request.method == 'POST':
                abort(403)
            token = secrets.token_urlsafe(32)
            while len(rows) >= 256:
                rows.popitem(last=False)
            rows[token] = dict(provider='jev', model='jev-latest', endpoint=JEV_ENDPOINT,
                candidate_id='', candidate_revision='0', form_nonce=secrets.token_urlsafe(32),
                enable_allowed=False, check_message='尚未检查此方案。', checked=None, initialized=False)
        row = rows[token]
        row['touched'] = now
        rows.move_to_end(token)
        g.decision_cookie = token
        return row

    @blueprint.after_request
    def decision_cookie(response):
        if hasattr(g, 'decision_cookie'):
            response.set_cookie(cookie, g.decision_cookie, httponly=True, samesite='Strict',
                secure=request.is_secure, max_age=3600, path='/settings')
            response.headers['Cache-Control'] = 'no-store'
        return response

    @blueprint.context_processor
    def decision_context():
        if request.endpoint != 'settings.page':
            return {}
        with lock:
            row = current()
            active = cloud = None
            legacy = 'unconfigured'
            try:
                if sys.platform != 'win32' and hasattr(service, 'decision_state'):
                    state = service.decision_state(include_cloud=True)
                    active, cloud = state['active'], state['current_cloud']
                legacy = service.jev_state() if hasattr(service, 'jev_state') else 'unconfigured'
                saved = bool(cloud) or legacy != 'unconfigured'
                if not row['initialized']:
                    if active:
                        row.update({k: active['profile'][k] for k in ('provider', 'model', 'endpoint')})
                    row['initialized'] = True
                status = 'configured' if active else legacy
            except SettingsError:
                saved, status = False, 'unavailable'
                row.update(enable_allowed=False, checked=None, check_message='检查未通过；原方案保留。')
            view = {k: v for k, v in row.items() if k not in {'checked', 'touched', 'initialized'}}
            view.update(cloud_key_saved=saved,
                active_provider_label=('TypeSafe' if active['profile']['provider'] == 'jev' else '本地 Clef') if active else ('TypeSafe' if legacy != 'unconfigured' else '-'),
                active_model=active['profile']['model'] if active else ('jev-latest' if legacy != 'unconfigured' else ''),
                state=status)
            return {'decision': view}

    def parsed(row):
        origin = request.host_url.rstrip('/')
        host = urlsplit(origin).hostname or ''
        if (not (host in {'127.0.0.1', 'localhost', '::1'} or host.endswith('.localhost'))
                or request.headers.get('Origin') != origin
                or request.headers.get('Sec-Fetch-Site') == 'cross-site'):
            abort(403)
        if request.mimetype != 'application/x-www-form-urlencoded':
            abort(415)
        if request.content_length is None or request.content_length > 4096:
            abort(413)
        allowed = {'return_to', 'candidate_id', 'candidate_revision', 'form_nonce', 'provider', 'endpoint', 'model', 'api_key'}
        if set(request.form) != allowed or any(len(request.form.getlist(k)) != 1 for k in request.form):
            raise SettingsError('decision_profile_invalid')
        if not secrets.compare_digest(request.form['form_nonce'], row['form_nonce']):
            abort(403)
        if not re.fullmatch(r'[0-9]{1,9}', request.form['candidate_revision']):
            raise SettingsError('decision_profile_invalid')
        fields = {k: request.form[k] for k in ('provider', 'endpoint', 'model')}
        return fields, request.form['candidate_revision'], request.form['api_key']

    def back(message):
        # Keep the existing return navigation; never redirect to an external URL.
        target = request.form.get('return_to', '/')
        if not target.startswith('/') or target.startswith('//') or '\\' in target:
            target = '/'
        return redirect(url_for('settings.page', open='models', message=message, return_to=target), code=303)

    def check():
        with lock:
            row = current()
            try:
                fields, revision, key = parsed(row)
                row.update(enable_allowed=False, checked=None, candidate_id='')
                row.update(fields, candidate_revision=revision)
                # Only an explicit browser check supplies network transports.
                # Copy avoids mutating shared service dependencies during a check.
                backend = copy(service)
                import httpx
                backend._decision_post = service._decision_post or httpx.post
                backend._decision_get = service._decision_get or httpx.get
                receipt = backend.check_decision_candidate(fields, api_key=key or None)
                row.update(candidate_id=receipt['draft_id'], checked=dict(fields=fields, revision=revision, receipt=receipt),
                    enable_allowed=True, check_message='检查通过，尚未启用。')
            except SettingsError:
                row.update(enable_allowed=False, checked=None, candidate_id='', check_message='检查未通过；原方案保留。')
            return back('jev_decision_check')

    def enable():
        with lock:
            row = current()
            try:
                fields, revision, key = parsed(row)
                checked = row['checked']
                if (not row['enable_allowed'] or not checked or key
                        or fields != checked['fields'] or revision != checked['revision']
                        or request.form['candidate_id'] != row['candidate_id']):
                    raise SettingsError('decision_profile_unvalidated')
                receipt = checked['receipt']
                service.activate_decision_candidate(receipt['draft_id'],
                    expected_active_id=receipt['expected_active_id'],
                    expected_current_cloud_profile_id=receipt['expected_current_cloud_profile_id'])
                row.update(enable_allowed=False, checked=None, check_message='方案已启用。')
            except SettingsError:
                row.update(enable_allowed=False, checked=None, check_message='检查未通过；原方案保留。')
            return back('jev_decision_enable')

    blueprint.add_url_rule('/settings/decision/check', 'check_decision', check, methods=['POST'])
    blueprint.add_url_rule('/settings/decision/enable', 'enable_decision', enable, methods=['POST'])

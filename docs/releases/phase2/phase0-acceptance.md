# 二期第 0 阶段验收：V1.3 收尾（仅 macOS）

日期：2026-09-29。依据：[二期施工计划](../../roadmap/phase2-plan.md)第三部分第 0 阶段、本地交接《V1.3 使用期 Bug 归纳与二期 Mac 收尾交接》第四、五、七节。

- 源码：`phase2/foundation`，第 0 阶段代码提交 `ff2b5b9`；Codex 路径修复见本文第 10 项。
- 候选：`2026.09.29.1`（产品版本 2.0），用现有本地签名配置构建，未发布、未安装、未替换正式应用；`verify_candidate.py --platform mac` 通过（deep strict 签名、运行时探针、两次启动）。
- 环境：macOS 27.0（26A428），Apple Silicon，Python 3.11.16，Google Chrome 154，Node 26.3.0，Codex CLI 0.157.1。
- 全量测试：2739 通过、36 跳过、0 失败（改动前基线 2644 通过、17 失败）。
- **Windows 未测试、未构建、未维护**（2026-09-28 平台裁决）。本文的通过都只指 macOS。

证据分四级，报告中不互相冒充：单元/合成（pytest）、隔离集成（源码测试实例与独立数据目录）、正式打包（上述候选 `.app`，独立数据目录）、真实来源（用户提供的公开抖音作品和测试专用登录会话）。截图、探针报告和运行日志含本机路径，保存在本地私有目录 `项目治理/执行核验/二期-20260929/phase0/`，不进仓库。

## 1. BUG-20260922-01：X 图片 OCR 在第一张图失败

1. **改动与理由**：`v1/vision_ocr.py` 的 `VNImageRequestHandler` options 由 `{}` 改为 `None`（nil）。本机复现：PyObjC 12.2.2 + macOS 27 传 `{}` 抛 `NSInvalidArgumentException - key does not exist`，传 nil 正常。同时拆分诊断阶段：request_setup、handler_init、perform_request、parse_observations、validate_coordinates。`OcrError` 增加阶段、异常类名、ObjC 异常名；`ocr-diagnostic.json` 与打包探针报告带上这些字段，不含图片文字、路径或原生异常消息。对外错误码与文案不变。
2. **根因测试**：`tests/v1/test_vision_ocr.py` 用桥接层行为模拟 macOS 27，并用真实 Vision 跑 4 张 1179 宽的 X 尺寸 JPEG、PNG、WebP、EXIF 方向 6 与空白图。把 options 改回 `{}` 后，5 项失败；修复后全部通过。`tests/v1/test_xpost.py` 验证第一张图桥接失败后的诊断记录，以及重试复用已下载的 4 张图（不重新采集）。
3. **正式 `.app`**：用候选包的 `--check-runtime --check-ocr-image` 在冻结应用中逐张识别，结果为 4 张 X 尺寸 JPEG 各两行、PNG、WebP、EXIF 方向图全部识别；空白图返回合法空结果；引擎为 apple_vision，运行时 macOS 27.0。
4. **结构与生命周期**：无数据库、任务状态或缓存格式变化。OCR 检查点键不变。
5. **定稿 UI**：无改动。
6. **未验与风险**：未用真实 X 帖子端到端重跑。该链路用合成 X 帖加真实 Vision 与打包探针覆盖。
7. Windows 未测、未构建。

## 2. BUG-20260922-02：抖音显示已登录，但来源仍获取失败

在测试会话副本上查实四层原因（均为本机真实运行）：

- **R1 登录未落盘**：扫码登录后，应用在读取 Cookie 快照后立即用信号结束专用 Chrome，`sessionid` 等认证 Cookie 从未写入专用资料。只读核对测试会话的 Cookies 库，只有 `ttwid`、`passport_csrf_token`。此后每次无头启动专用浏览器都是登出状态，抖音返回 `status_code 8 用户未登录`。
- **R2 设置只看本地资料**：数据库 connected 加本地 Cookie 快照，就显示"已登录"。
- **R3 错误分类**：浏览器接口的 `status_code 8` 被当成 `collection_upstream_failed`；下载失败一律变成 `douyin_source_unavailable`。
- **R4 下载器自取详情被拒**：专用浏览器已登录并读到作品详情后，安装的下载器仍自己再用未签名 HTTP 请求一次详情，抖音返回 403，于是报"没有取得完整来源内容"。这与用户现场症状一致。

1. **改动**：`v1/douyin_session.py` 关闭专用 Chrome 时先经 DevTools `Browser.close` 正常退出，让 Chrome 落盘 Cookie，失败再退回原结束方式（仅 macOS/Linux，Windows 路径不变）。新增 `live_status()`，在专用资料里只读请求本人资料接口：0 为已登录，8 或 401 为登出，其余为未知；不保存、不替换、不清除凭据。`v1/douyin_collection_browser.py` 三处读取把 `status_code 8` 映射为 `douyin_login_required`。`v1/douyin.py` 把内部失败拆为子码：no_saved_session、session_logged_out、downloader_login_required、item_unresolved、detail_missing、detail_identity_mismatch、download_failed、media_missing、media_ambiguous、downloader_unavailable、upstream_exception。下载器通过 `_VerifiedDetailClient` 使用浏览器已核对身份的同一份作品详情。管线把子码写入 `source-diagnostic.json`。设置页：`platform_health()` 与 `/settings/platforms/douyin/health`，只有确定登出才改为需重新登录，且只改检查时的那一代连接；结果缓存 60 秒。失败卡的 `douyin_login_required` 加"前往设置"。
2. **根因测试**：`tests/v1/test_douyin_login_health.py` 共 28 项，旧源码下 24 项失败。覆盖 8 号码映射、原生详情登出、下载器拿到的是已核对详情的副本、各子码、诊断记录、实时校验不动凭据、优雅退出先于信号、退出失败时的回退、设置状态与竞态、设置页浏览器渲染三种结论。
3. **隔离集成与正式 `.app`（真实会话）**：
   - 登出状态的专用资料：打包应用 1.2 秒完成实时校验，设置页显示"需重新登录"并切换为"重新登录"按钮。提交用户提供的分享文本，投递栏直接提示"需要重新登录抖音后再试。"，不再出现泛化的来源不可用。
   - 用测试手段恢复会话：把登录时保存的 Cookie 快照写回克隆出的专用资料，并在测试库中恢复连接状态，以此代替用户扫码重连。之后健康检查为已登录。同一短链接完整采集（浏览器详情加下载，约 84 MB）。Qwen 转写、Codex 审阅与提炼后生成 1 条知识和 1 篇来源笔记。其间第一次因打包应用找不到 Codex 失败（见第 10 项），修复路径后对旧任务点"重试"即完成。
   - 再次提交同一短链接：新任务复用同一素材，知识仍为 1 条，笔记仍为 1 篇。
   - 用 `Browser.close` 退出后，专用资料的 Cookies 库确实写入了 `sessionid`、`sid_tt`、`uid_tt`、`sid_guard`；用信号结束时这些都没写入。
4. **结构与生命周期**：无数据库结构变化。连接状态仍是原有三态，"正在校验/已配置"只是展示态。新增本地诊断文件，随任务目录按现有 72 小时规则清理。
5. **定稿 UI**：设置页平台行沿用现有 `state-dot` 各变体、`status-text` 与既有按钮，新增"正在校验""已配置"两段状态文字；失败卡复用已有的"前往设置"链接。依据是开工确认单中用户确认的第 0 阶段条目"设置页实时、非破坏性校验"与交接文档 A2 建议 1。没有新增样式。
6. **未验与风险**：产品自己的扫码重连流程没有在无人值守期间实跑，以上"重连"是测试手段，扫码重连后重试已列入用户回来后的清单。计划要求的"在测试会话中登出、制造真实过期样本"放在第 4 阶段最后一次真实端到端之后执行，避免提前失去测试会话。其他平台（X、YouTube、微博）仍只看本地连接资料，已记入待办。
7. Windows 未测、未构建。

## 3. BUG-20260916-01：整理启动后按钮被错误重新启用

1. **改动**：`static/home.js` 提交处理只在请求未被接受时恢复按钮；被接受的响应已经带来服务器的状态（运行中为禁用），`finally` 不再覆盖。失效确认卡的规则保留。
2. **根因测试**：`tests/v1/test_home_organization_state.py`。鼠标单击、双击、Enter、Space 四种方式下，禁用都在同一任务内立即生效；请求被接受、经过一次轮询和刷新后，仍为"正在整理"且禁用，悬停不出现可操作样式；只发出一次 POST，只有一个事件。另测启动失败（503）后按钮恢复，以及四线程并发、两标签重复 POST 都只产生或复用一个事件（`one_running_organization_event` 唯一索引）。前 4 项在旧源码下失败。
3. **正式 `.app`**：隔离 Chromium 驱动候选包首页，失败和成功两条路径下都是同一任务内禁用，接受后并经轮询仍为"正在整理"且禁用，整理结束后区域按服务器状态收起或恢复"开始整理"。
4. **结构**：无变化。
5. **定稿 UI**：样式、文案、布局不变。
6. **未验**：Safari 与用户日常 Chrome 中的真实点击未测。
7. Windows 未测、未构建。

## 4. BUG-20260916-02：整理失败提示跨刷新残留

与第 3 项同模块施工，分别验收。

1. **改动**：`organization_status()` 增加 `latest_event`。首页提示节点带事件编号和是否失败两个属性。历史失败按隐藏渲染，读取故障照常每次显示。`home.js` 以页面加载时看到的事件为本页基线，只显示本页打开后新出现的失败，包括加载时正在运行、之后失败的那一次。每个标签页各自维护基线。失败事件、失败码和诊断照旧保留。
2. **根因测试**：同上文件中的历史保留、读取故障常显，以及浏览器中"只显示本标签亲眼看到的失败"：新标签不显示，原标签不被其他标签静默；刷新后消失。运行中后失败的情形同样覆盖。旧源码下相关项失败。
3. **正式 `.app`**：用无效模型制造一次真实整理失败（topic_planning_failed）。正在观察的标签显示提示，按钮恢复为可点击的"开始整理"；此后新开的标签不显示提示，原标签仍显示；原标签刷新后提示消失；数据库保留失败事件。换回有效模型后，第二次整理成功，历史失败没有在新加载时显示。
4. **结构**：无数据库变化，事件不删除。
5. **定稿 UI**：提示位置、样式、文案不变。
6. **未验**：Safari 未测。
7. Windows 未测、未构建。

## 5. BUG-20260915-02：冷启动文档模型检查提示不消失

1. **改动**：`templates/base.html` 的提示带 `data-document-component` 状态，就绪时也渲染为隐藏节点。`static/updates.js` 用轮询得到的 `document_component` 同步这一节点：ready 时隐藏，unavailable 时显示修复指引；仅手动更新模式下也会单独轮询到检查结束。完整性校验本身未改（按 Q2 裁决，不降低醒目程度，只修状态同步）。
2. **根因测试**：`tests/v1/test_document_component_notice.py`。首页、设置、主题、新知四页的状态属性；轮询接口；浏览器中从 checking 自动变为 ready 并隐藏、再变为 unavailable 并显示修复指引，其间不刷新、不导航，首页草稿保留。旧源码下 5 项失败。
3. **正式 `.app`**：候选包冷启动后 0.67 秒首页首屏显示"正在检查本地文档模型"，约 4.8 秒后在同一页面自动隐藏，URL 不变，草稿保留。在测试副本中替换一个模型文件（新文件替换，不改动正式副本）后，同样从 checking 自动变为修复指引，设置页一致。之后恢复文件。
4. **结构**：无变化。
5. **定稿 UI**：未改提示样式和文案。顶部总状态的"正常"指任务队列，提示本身写明只影响文档任务；按 Q2 不做进一步改动。
6. **未验**：每次启动约 1.2 GB 校验的性能优化是独立议题，未做。
7. Windows 未测、未构建。

## 6. BUG-20260917-01：疑点卡没有局部原音，点击恢复仍失败

按 Q4，拿不到现场证据，只补诊断，不盲修。**结论：诊断能力已补，真实根因待映射。**

1. **改动**：新增 `v1/audio_diagnostics.py`，按任务保留最近 20 条脱敏记录，只含阶段码、疑点内部音频名、异常类名。阶段码有：initial_clip_failed（说明恢复入口为何出现）、timeline_missing、source_audio_missing、relocation_failed、clip_failed、write_failed、state_save_failed、serve_failed（missing_file、realign_failed 或 symlink）、playback_failed（浏览器 MediaError 码，经新的只写诊断接口上报）、recovered。`ConfirmationAudioError` 带内部步骤（locate、ffmpeg、invalid_output、write）；对外错误码不变。
2. **测试**：`tests/v1/test_audio_recovery_diagnostics.py` 共 16 项，用合成音频与临时数据逐条命中每个阶段，确认失败后疑点文字、候选和已有判断原样不变，记录中不含正文或候选；另用真实浏览器验证无法播放时的上报。
3. **正式 `.app`**：本项没有打包运行。缺少能稳定触发真实恢复失败的样本，而打包路径与源码路径一致，属于可诊断性改造。
4. **结构**：无数据库变化。任务目录多一个诊断文件，随任务目录清理。首次裁剪在建目录时失败，现在也归入可重试的恢复入口，过去这里会抛出原始 OSError。
5. **定稿 UI**：卡片文案、按钮和交互不变。
6. **未验**：朋友现场的真实分支尚未映射，需要对方提供脱敏截图、诊断文件中的阶段码和系统版本。
7. Windows 未测、未构建。

## 7. BUG-20260915-01：Dock 找回已有页面（方案③）

用户 2026-09-29 选方案③：不加权限。V1.3 已有的协议即为该方案的实现，本轮没有改动选页逻辑。

- **承诺边界**：写入 [README](../../../README.md) 与 [桌面文档](../../engineering/desktop.md)。页面都关闭时重开一页；页面仍在时只激活浏览器并请求显示，不保证切到后台标签；未显示时弹出"重试显示/另开产品页面/取消"。多标签时，最近一次可见且聚焦或有真实交互的页优先，否则取最早打开的一页；已有页面不重载、不改输入。
- **隔离浏览器验证**：`tests/v1/test_dock_option3_browser.py`，用 Playwright 自带 Chromium，不是用户的日常 Chrome。覆盖多标签选中最近使用页且三页草稿不变、关标签、关窗口（逐页卸载）后只重开一页并复用、页面无 pagehide 消失时返回未知而不另开、浏览器退出后仅凭原生退出信号退役。
- **边界说明**：Playwright 会把每个页面都模拟为可见且聚焦，无法复现"后台标签"；这一判定由单元测试的 hidden ACK 覆盖。
- **待用户**：用户日常 Chrome 与 Safari 中的真实程序坞点击，包括退出浏览器，在回来后的清单中。
- Windows 未测、未构建。

## 8. BUG-20260914-09（Q9）：组件资源 404 被误报为清单缺失

核对结论：已于 V1.3（`e79bd56`）在模块层修复。组件下载失败带真实资源角色，只有清单 404 才显示"当前发布尚未提供此平台的发行信息"。本轮在 `tests/v1/test_component_bootstrap.py` 补安装页层回归：基座、Docling、差量的 404，网络中断，哈希不符，页面都指向实际资源和阶段，不出现清单缺失，签名 URL 参数不外露，目标程序未创建。按计划关闭。原生安装器实跑未做。

## 9. Q11：14 项既有测试失败逐项判定

全部判定为过时夹具或被取代的断言，没有产品回归；理由写在各测试文件内。

| 测试 | 判定 | 处理 |
|---|---|---|
| `test_vision_ocr` 真实 Vision | 夹具：macOS 27 已无 PingFang.ttc；STHeiti Medium 的"器"被识别为异体"噐" | 改用系统冬青黑体（Hiragino Sans GB），预期文字不变 |
| `test_reading_style` 时区 | 夹具：显示值是读者本地时间，断言假设 +08:00 | 测试内固定 Asia/Shanghai |
| `test_same_topic_intake` 7 项 | 断言过时：回执状态尾注（BUG-20260914-06）使确认按钮不再是最后一个元素 | 按动作名定位按钮 |
| `tests/test_web.py` 2 项 | 夹具：未注入提炼器，旧版应用按环境变量构造真实 LLM，结果依赖机器，还可能调用付费模型 | 注入测试文件已有的静态提炼器与判定器 |
| `test_llm` 复用 | 断言过时：`1293fa5` 起，请求自有回执保留模型原始响应，改动的证据副本不会被复用，完好的回执会被重新校验 | 改为：证据副本被改时不重复计费；回执也被改时重新调用，结果为模型真实输出 |
| `test_feishu_scopes` 标题 | 断言过时：`c838cc8` 按 2026-09-15 用户裁决恢复 V1.2 普通疑点卡，标题为"有内容待你确认" | 更正断言，并在 feishu-status.md 写明这一例外 |
| `test_home_polling` | 夹具：`closest:()=>true` 使每个表单都像失效卡；Node 26 的 vm 不再允许从外部替换脚本内的函数声明 | 桩只对 `#home-results` 返回；在上下文内安装假函数；变异检查确认仍能抓住"过期轮询覆盖提交结果" |

## 10. 新发现：打包应用找不到 Codex（BUG-20260929-01）

打包应用把 PATH 限定为包内 bin 加系统目录。本机 ChatGPT.app 在 2026-09 把 CLI 移到 `Contents/Resources/codex-cli/bin/codex`，旧位置已不存在；Homebrew 版在 `/opt/homebrew/bin`。因此候选包和当前正式 V1.3 在本机都会以 `llm_config_unavailable` 失败；正式应用近期日志中没有模型调用记录，所以未被察觉。修复：在 macOS 上依次查找 Homebrew、`/usr/local/bin`、`~/.local/bin`，再找 ChatGPT.app 的新、旧位置和 Codex.app；用户自己安装的 CLI 优先。测试见 `tests/v1/test_codex.py`。候选包 `2026.09.29.1` 的验证用 `KNOWLEDGE_DISTILLER_CODEX` 显式指定路径，这是已有的受支持方式；第 4 阶段的候选包含此修复。

## 11. 观察（未改动，待用户决定）

飞书回执在待操作状态下，尾注"请完成下方当前可执行的选择或确认。"位于按钮之后，而文字说的是"下方"。这属于已定稿的飞书卡片文案，本轮未改，记入待办。

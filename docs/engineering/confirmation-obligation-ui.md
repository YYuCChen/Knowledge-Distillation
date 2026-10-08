# 判断前准备义务与入口撤除

2026-10-08：用户明确将局部原音恢复、结合上下文补充候选判定为程序呈现判断前的义务；主控批准删除两个独立入口及删除后的最小布局调整。

本次只移除首页两段独立 POST form、对应条件和闲置 CSS。保留 operations/actions/manual/candidate 容器身份、候选解释、原生播放器、重新识别、查看来源、自定义输入、无法确认、提交及原 token/revision 合同。home.js 不变，旧 API 和 JS 分支的存在不代表仍有页面入口。

此前“宽窗自定义整行”是只沿代码恢复的历史方案，未逐项核母版，已被用户最新具体母版恢复裁决取代。保留其隔离展示和失败记录，不把历史展示当母版验收。

最新母版六态恢复：中文局部上下文和短词 inline 候选、展开/空白错误；英文原句 header、整句候选和中文释义、逐候选原生 details 判断依据、右侧“保留原文/采用”、下方独立疑点原因。宽窗原音整行，来源130px/弹性自定义/122px判断同行；720px 以下沿已批准适配为原音、来源、自定义整行、判断右下。使用既有字体、控件、32px原生播放器、16px来源行框与10px gap，未重造首页或播放器。自定义宽字段保留母版修订的20px尾部空位，窄窗收回以保持整行。

读取的直接证据：本地设计母版10/12/29/34脚本、Tesla清点报告、mcp38/63历史metadata、mcp89两个中文组件layout-node、mcp37/62真实缓存PNG及用户新六态截图。没有新的云端get_design_context成功，不将历史结构/图片称作完整线上高保真。30-narrow只有800px页面，不证明360六态；360采用原U05批准的局部顺序，最终几何仍须真实浏览器核验。

纯函数 english_candidate_display(snapshot, concern)严格核验sentence_span、来源精确span、完整原候选集合及candidate_translations/candidate_basis逐项映射。展示替换仅在当前span内，提交仍是原candidate值，不猜句边界、不全局replace、不插候选。旧candidate_explanations保留但不冒充整句译文和独立依据；concern.reason只展示一次，不能复制到各basis。准备字段归后台owner，web._item_view接线归Lagrange，本UI不改它们。

英文candidate form与details用UID作用域加纯projection语义hash作稳定DOM身份，替代form旧loop序号，避免候选重排时依据跨父节点丢失/串错；POST路由、字段、token/revision、UID、manual/actions/operations/audio身份不改。hash包含候选、原句、展示句、译文及依据，语义更新换身份，原生details不会继承旧证据状态。复用现home.js对稳定节点和details.open的保护，不改U04焦点/草稿/媒体逻辑。

空manual仅在当前准确空白错误“请输入正确文字”且保留值为空/空格时映射母版输入提示“请输入确认文字”和独立红说明“请输入确认文字后再提交；也可以选择候选或回听原音。”，以data-empty-confirmation局部标记控制红线。其它实际错误仍显示原message和值，不按关键词猜空白，不更改backend错误合同。

UI 删除不实现后台准备。不得由旧 waiting_user、audio_recovery_required、空解释、路径存在或 SSR 成功推导准备通过。后台 owner 应提供准备与校验结果；失败沿既有 failed/error_code/retryable/needs_settings 展示，不在模板猜测状态、不新增准备按钮或错误文案。旧不完整待办及草稿的迁移仍须后台实际合同。

独立tmp_path SSR设计保护两入口不存在、候选POST原值、整句/译文/独立依据、字段与媒体身份、中文inline及准确空白错误；纯projection设计覆盖重复词、版本语义身份、Unicode边界、不完整映射拒绝。不证明浏览器几何或后台就绪。SSR依赖Lagrange实际view接线，不能用测试内注入display假装产品已接线。旧隔离展示只使用合成声音和排布解释，不是识别/准备证据；本轮不启动新六态demo、不执行pytest，后台真实测试和主控截图复验仍待完成。

2026-10-08 原生定位追加（前文 home.js 不变属于此前阶段）：主控实际 IAB v2/59920 已确认中文 folded actions 隐藏、宽窗原音32px与来源/自定义/判断同行；实际空白提交却被旧 home.js 在 fetch 前拦截，只出现旧 placeholder，没有完整母版错误行。最新具体授权只改原空白 submit 分支和 input 清错事件，模板常驻原稳定 key 的隐藏错误段；blank 时设置既有空白标记/aria-describedby并显示完整原错误文案，保留空白输入与原 input.focus，编辑后隐藏说明、清除关联并恢复中文/英文原 placeholder。poll/reconcile/焦点媒体/队列逻辑逐字不动；非空真实错误仍走原 message/value/POST 合同。SSR 只同步常驻节点断言，controlled synthetic Store helper/proof不变。已做 JS/Jinja/Python 语法与窄diff静态检查，尚未经过新候选的原生复验或pytest，不宣称六态已通过、真实准备语义通过或发布。

同次原母版10-confirmation.js:31来源动作130px仅两个UI/Small纯文字，未放外链图标；据用户按原稿还原范围，仅删除确认卡footer“查看来源”a内旧img，文字、href、target/rel和可访问名称保持，其余source链接/全局图标/CSS不改。后续v3隔离候选必须把新home.js纳入六overlay及live guard，与home/CSS/operations/web/collection准确字节绑定；仅新私有候选、合成材料和UI-only receipt，不安装/发布/产品后台接线。59920及各失败截图/合成材料保留历史，服务已按ownPTY停止；新v3尚待主控清单审阅与明确internal启动授权。

2026-10-08 最终本轮验收追加：以上“尚待”是历史时点。主控独立核v3的182项source、10项local、6项live overlay后授权私有合成服务50179；实际IAB在1280/800/360窗口核中文折叠、展开、空白错误及英文折叠、展开、独立依据展开六态。宽卡636px、原音506×32、自定义214px；两来源动作同16px行框。窄卡294px、原音204×32、来源并排、输入204px、判断靠右，无横向溢出。实际空格提交保留两空格并显示准确母版提示及11px/16.5px说明；编辑后隐藏说明、恢复各语言默认placeholder并保留草稿/焦点。英文依据原生details独立展开，跨轮询间隔与窗口变化保留。

窄窗菜单首次鼠标测试因工具重置viewport误点音量，未计通过；保留该截图。随后沿原生audio键盘Tab到更多菜单，Enter实际展开menu，DOM读回窗口360且无横向溢出，Escape关闭；没有DOM赋值伪造状态。18幅六态截图和实际菜单证据见根工程docs/evidence/confirmation-obligation-20261008/master-v3.md。截图API返回JPEG，归档按真实格式命名.jpg，像素未编辑。临时viewport已reset。

唯一SSR首轮在固定a7a01aa加六准确overlay的独立0700根S0tmMw运行15节点，15passed、0failure/error/skip、pytest/外层0；主控独立读JUnit SHA54ec87e76912ea3c8424654c16bdc488c15a2819b56e5eaf1825d0103cbd3c54及raw退出/六源码字节。测试使用实际Store、完整共享proof与合成PCM，未带并行schema25。SSR不代替原生几何，合成例也不证明模型语义或正式发布。本轮没有安装/替换/重启正式本机程序、真实DB/Vault迁移、模型部署或公开发布。

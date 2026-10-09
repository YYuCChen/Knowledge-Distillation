# R16 纯身份上下文候选

2026-10-08，Asia/Taipei。模块为 `knowledge_distiller.v1.capture_identity_context`，合同 `capture-identity-context-v1`。仅新增独立模块、合成测试与本文；未接 Captures/schema/DB/raw/decision_client/profiles/ingestion/UI。未运行 pytest、HTTP、模型或服务；原 R12 三文件冻结不改。模型默认 proposal_only，不能因此宣称附言 bug 已在产品修复。

## 输入与来源绑定

`CaptureSource` 持有 frozen source key（app/message/capture/version）、原 UTF-8 bytes 和 SHA-256。prepare 校验 bytes/hash/合法 UTF-8，保持 CRLF、前后空白、Unicode 原字；不 strip、规整、分块或用长度/格式推断作者。其键和版本由原 Captures 的未来只读投影显式提供，不由标题推导；hash 证明输入完整性，不证明平台传输来源真实性。若原平台字节没有被留存，不能从 captures.text 反证原传输编码。

`TargetCandidate` 保留真实既有 app/message/part/version、title、已有 summary 及 provenance，无 summary 可明确 None，不发生成请求。稳定 candidate_id 为 `target:` + SHA256(JSON([app_id,message_id,part_id,version]))，不由标题/摘要生成、不分配 raw 编号。一个 receipt 多个 parts、同标题不同版本都有不同 ID。禁止跨 app 候选、同 key/version 内容矛盾、无归属摘要、可变 evidence/literal_refs 容器。

`ReferenceEvidence` 必须精确绑定 source_sha256、target_id、target_version、provenance_ref。reply_metadata/explicit_user 是调用方提供且负责真实性的来源证据，不是模型新造事实。literal 还必须绑定原文 code point char 半开范围，全文该片段必须精确等于 candidate_id 或该目标已有稳定 ID/URL alias；alias 限 R-* 编号或 HTTP(S) URL，不允许“这篇”等主题文字作为稳定绑定。SHA、目标或版本错、char 范围错均不能支持 target。

topic/adjacency 只用于提名背景，不满足确定目标证据。模型选择一个 target ID 后仍需上述绑定检查，否则 relation=unknown、target_id=None，保留模型原 nomination 和原因；不把最近投递或标题相似当附言对象。candidate_id 是本合同的源投影标识，不是新知识库或来源编号权威。

## prepare / request / resolve

`prepare_identity_context(source, targets=(), prior_user=None, user_override=None, profile=None, profile_version=None, scope_complete=True, excluded_count=0, max_candidates=8)` 是纯函数。profile 仅为调用方显式提供的既有不可变 DecisionProfile 值；模块不访问 DecisionProfiles、不解析 secret、不验证或激活 profile，也不创建客户端。缺 profile 时不生成可发送 request，保留 pending/no_profile；完整有效用户裁决仍不需要 profile。

prepare 固定 source/context/profile-version 和 prior/override provenance，序列化不可变 state/questions bytes，算 context SHA；body 和两组 choice 均进入 context，遗漏候选清单只以本地 hash 绑定，不向模型灌无限遗漏 ID。

`prepared.request()` 返回新的 `(state, dict[str, ChoiceQuestion])`，包括 author_identity 与 relation_target 两题，可供后续授权调用方在**一次**现行 `DecisionClient.ask(state, questions)` 中提交。模块自己不 ask、不发 HTTP。每次返回新 dict，调用方修改不会改 prepared 冻结正文/schema/hash。

- author_identity：self / third_party / mixed / unknown。发送者不是作者，格式/长度不是作者依据；引用与本人说明混合可 mixed，未知保留。
- relation_target：independent / unknown /最多 N 个精确 target ID。零候选仍有两个选项，不需要 has_related 布尔。关联与作者不是同一判断；模型联合评分不表示统计独立。

`resolve_identity_candidate(prepared, snapshot, reply=None, error_code=None)` 显式接收当前 `ResolutionSnapshot(source_sha256, source_identity, source_version, prior_event_id, target_versions, context_sha256)`。source_identity 是精确 `(app_id, message_id, capture_id)`；同正文 bytes/SHA 但 namespace 或 version 不同仍 pending，不产生投影或 supersede。snapshot 必须由未来当前读投影/事务提供，prepared.snapshot() 仅为该 prepared 的快照便利方法；将旧 prepared.snapshot() 重复当成“当前状态”不能验证真实数据库。

target_versions 必须覆盖**完整当前 selected 集合**，不得只提供最终 target 或集合子集，缺项/加项/重复 ID/版本变化使旧候选失效；集合成员顺序可以不同。snapshot.context_sha256 必须来自同一当前冻结上下文，核对已有 prepare context hash（不新增缓存/存储层）。当前 source/用户事件、profile、selected IDs/version/title/summary/evidence、scope complete/omitted_count/reason、遗漏 ID清单 hash 发生变化均必须重建 context，旧候选失效；selected 排序是 prepare 的稳定排序，不能通过任意外部顺序引入新身份。不在传入范围内且调用方从未声明的变化无法由纯模块检测，调用方必须提供真实当前覆盖及版本，不能拿旧 hash 对自己确认安全。模型 proposal_only 也不等于 CAS 安全：共享 DB 接线仍需事务内重新读取并核验 source identity/version、事件及目标范围，不止核正文 SHA。

模型答复使用 `BoundModelReply(context_sha256, source_sha256, profile_version, DecisionResult)`：检查三项绑定、provider/protocol/requested model、Clef 实际 model、两题全集/类型/选项全集/confidence_semantics。概率合法性、usage 与预算/HTTP 的验证沿用现行 DecisionClient 返回的已验证 DecisionResult；不能绕 Client 用自由 wire JSON 假称模型实测。来源调用方为 envelope 真实性负责，hash 不把伪造结果变成真实请求证明。

结果保留原 DecisionResult 的 provider/model、完整概率、confidence/语义、usage/budget，deepcopy 与调用方可变 dict 隔离。Jev 为 jev-normalized-concentration、Clef 为 clef-max-probability，不沿用旧 0.9/0.8、不搬 confidence 门槛、不引 AcceptancePolicy 层。**所有模型结果，即使两题一致、高概率、目标证据完整，仍 proposal_only=True、needs_confirmation=True、legacy_projection=None。** unknown/mixed 没有 raw 操作。

## 用户覆盖、冲突与更正计划

`UserDeclaration` 需 source hash、actor_ref、evidence_ref；prior_user 还需 event_id，新 override 需 expected_prior_event_id 精确匹配当前 prior。用户 target 声明必须精确 target ID/version 存在于当前合法来源集；无 target 的 relation 不能夹带 target 字段。source/目标版本/事件过期返回可确认冲突，不回退最近投递。

有效 prior_user 先保留，新有效 override 仅替换明确声明维度；例如确认作者 self 不意味着 independent，确认 target 不意味着作者 self。新 override 基于正确旧事件可明确更正旧维度；基于错误 event/version/source 则不覆盖有效 prior。模型冲突记录 model_conflicts_with_user_author/relation，原用户维度不会被模型替换。完整用户裁决无需模型，不因未请求的模型超时/预算错误丢失其权威。

完整有效用户裁决：author=self/third_party 且 relation=independent/target，在当前 snapshot 无冲突时才可确定 `legacy_projection`。self+independent→my_thought；self+target→annotation；third_party 保持 third_party，关联证据仍独立。mixed/unknown 即使由用户选定也保持待确认，不混成纯本人原话；本文不改现语音默认本人和语音第三方限制。

新 override 对已有用户裁决造成作者/关联/target 变化时返回 frozen `SupersedePlan`，含 prior event ID/source hash/旧新维度与 target/context hash；annotation→annotation 更换 target 也产生计划。它不执行事件、schema、raw 写入或编号分配。实际 event CAS/append-only 与 RawLedger.supersede 接线需主控另行审阅；旧 raw bytes/用户笔记不动。

## 有限候选与预算

默认 N=8 为内部合同上限，不是设置/UI。精确用户目标、强来源证据先，再按稳定 ID；选择过程不扫描历史/DB/Vault。超 N、外部明确 excluded_count 或 scope_complete=False 时，state 明示 incomplete、遗漏数量与 bounded_candidate_scope 原因，本地留 omitted_target_ids。完整原标题/summary/原文不会裁剪；已有显式目标若被上限遗漏则不请求/不确定。模型 independent 在 incomplete scope 中强制 unknown；完整用户声明 independent 不依赖模型对已知候选范围推断。

Clef 直接复用现 `decision_client.clef_template_upper_bound`，传完整 state/两题/全部选项/model/truncate=False，与现 Client 使用同一公开函数和模板。它仍是 UTF-8 字节上界，不是 tokenizer 实测，超 profile.token_budget 则无 request/pending_budget，正文和摘要原字节仍在 prepared。不为预算静默删目标/summary，不自动分块或云回退。

**Jev 的现行预算接口限制**：`DecisionClient.ask` 内部使用 request UTF-8 字节保守准入，没有公开纯 estimator。本候选不复制该算法：prepared.budget=None，request 交授权调用方经原 Client 在 transport/auth 前执行准入，失败以稳定 decision_budget_exceeded 返回 resolve，保留 pending。budget=None 不能表述为“已通过 Jev 预算”；禁止绕过 Client 直接 POST。Client 的请求前预检也不证明云端总模板精确 token 数。双方原 response usage/budget 原样留在 model_result 中。

完整有效用户裁决不需要任何模型预算；长附言用户确认可确定而不发请求。无 profile、不完整上下文、错误/过期、超限均不触及正式数据或替换旧模型配置。

## 验证与产品边界

合成测试已编写：原 UTF8/CRLF/Unicode/hash、长附言与格式、四作者身份和两问一次 fake ask、引用/指代/版本绑定、literal 范围、recent/topic 拒绝、多 part/同标题/不同版本、prior/partial/过期用户覆盖、同身份 target 更正 supersede 计划、N=8/省略数量/不完整 scope、缺 profile、完整 Clef 模板预算/摘要不裁剪、Jev 原 Client 超限前拦截、两 provider 原语义、高概率仍 proposal、未知不 raw、快照/结果过期、请求可变副本隔离。**未运行 pytest，不报告通过数。** fake HTTP/secret 值全合成；代码测试未来运行也不接真实服务。

只进行了标准库 AST 解析与文件读回/字节指纹；无法由此验证行为。真实模型中文效果、平台 reply 投影真实性、候选摘要来源、旧事件上下文缺口、实际共享预算、schema/事务、raw 原文与 LF 派生谱系、target-only raw supersede、旧卡撤下/恢复、UI 条件和设置均未接入/未验。

主控完整静态审阅发现首稿 `_strong_reference` 错误引用局部未定义的 targets，非空候选会发生 NameError；此前 AST 解析成功没有检测这个运行期名称错误，不能当作合同正确的证据。已将不可变 targets tuple 校验移到 prepare 函数入口，从 helper 删除错误引用，保留既有目标用例并新增入口/非空目标回归。另新增同 bytes 不同 source identity/version、当前 selected 集合/范围/context 变化的过期回归。修复后只解析/读回，仍未运行 pytest 或行为测试。

对照来源：根 docs/requirements.md R16/R17；现 captures.py（全文）、capture_schema.py、database.RAW_STATEMENTS、raw.body/RawLedger.supersede；现 decision_client.py / decision_profiles.py；已批准五映射 source/docs/engineering/legacy-mapping.md。旧历史输入只能按该五规则私留/明确确认，不因本候选重分类或全历史重算。上述共享文件本任务全部不改。

## 2026-10-08 修复版行为复验

以上“未运行 pytest”保留为编码阶段历史。主控已读实际源码、测试及定向修复，随后 Gibbs（自身 JSONL 核验为 gpt-5.6-luna/max）仅运行该测试文件一次：固定 CPython 3.11.16、env -i、新 source PYTHONPATH、禁用自动插件和缓存，目录 `/private/tmp/kd-v3-r16-tests-20261008.0tBWBo`。命令 `python -m pytest -q -ra -p no:cacheprovider --basetemp=<该目录>/pytest --junitxml=<该目录>/results.xml tests/v1/test_capture_identity_context.py`，exit 0，51 passed，0 failed/error/skipped；未重试或放宽断言。

主控独立读回 JUnit：51 个实际 testcase，SHA256 `6b670e9be279441dab3ab08c1dd2915506641d255bc38daf3a17b05ed302e29e`。源码 SHA256 `031854c887f44bb40f6380cbacc9771b85bc284e19e69bd32f73753537ff9969`，测试 `4b2d3e7e62fed4aae02c8b498e104638718e5c030dbaa3f921cf3639064589f0`，执行前后相同。测试工具是旧工作区的受控解释器，不是本次完整构建环境。

结果仅证明合成输入下的身份、目标、预算和过期拒绝合同；所有模型结果仍为 proposal_only。没有生产 Captures/schema/raw/UI 接线、真实模型语义验收或历史重分类，未接触真实 DB/Vault/profile/凭据，未安装应用、启动模型或发布。当前快照必须由未来调用方重新投影；模块本身不是数据库并发事务证明。

# R14 raw→wiki 独立来源支持合同

当前实现核对：2026-10-09，Asia/Taipei，源码业务基线 `e4283034f05778020d159742d1db0a1d904f97cc`。`wiki_support.py` 保持独立 Gate API，已由 `WikiWorker._run_typed_locked` 经 `_typed_candidate` 接入实际 typed 整理链；`app.py` 注入同库 source_store，`WikiWorkflow.submit_all` 创建 `r08-wiki-outcomes-v1` 任务。它不调用已淘汰的旧 SQL derive。已接线不等于真实验收：按主控当前证据，真实 fresh 整链尚未 passed，actual accepted 仍为 0，发布未完成；本次仅读源码维护文档，未查正式数据或重跑测试。

历史证据（2026-10-08 独立模块阶段，不代表当前 HEAD 的整链验收）：主控已读完整初稿与修复差异，确认多跳引用经过变更页时重新核对、私有 checkpoint 及持有验证 fd 的锁边界。当时独立 Luna/max 修复回归为 95 passed、无失败／错误／跳过，pytest 退出 0；测试结束后的 zsh 包装命令因误用只读变量 status 退出 1，保留此包装失败，不重跑已通过测试。证据 `/tmp/kd-v3-wiki-support-fix-tests-20261008.md`，目录 `/private/tmp/kd-v3-wiki-support-fix-tests-20261008.ci0bQz`，JUnit SHA256 `b04a683eca5bf8671e60a881bb2e885de7f9ac3f9248f0e7026352bf65299d7b`。主控另读实际 JUnit 与 SHA；初次内部提交 `ce1cca0dd67c51c3c113eb8aed078ace8e59939f`。当时只确认合成候选支持合同，worker 接线尚未完成；当前接线状态以上段为准。旧 73 pass 同属历史 fake 控制链证据。现行主控为 gpt-6-astra/medium，全部子 Agent 为 gpt-6.1-sol/medium；旧 Luna/max 仅记录历史执行者。

## 输入与 API

`DocumentChange(path,before,before_sha256,after,after_sha256)` 保存完整 bytes 和明确 SHA256。新建的 before 两值均为 None；删除不属于本合同。调用方在独立 staging 完成候选后调用 `build_registry(staging_root, changes, raws, pages=(), generated=(), max_depth=4, parent_registry=None, claim_mapping=(), program_facts=None, program_facts_readback=None)`。所有 after、raw 及依赖页都必须与 staging 常规文件逐字一致；before 必须为冻结基线 bytes，worker 从 batch checkpoint 的 before 目录读取。程序检查 UTF-8、摘要、路径及符号链接，绝不写 raw。

`FrozenRaw(path,stable_id,content,sha256)` 给出完整 raw，编号与文件名／信封相同，身份只能第三方／本人／本人附言，路径身份一致，不创建或修改正式 ID。缺作者保存为“未知”，不猜作者。重复 raw ID、重复 source anchor、已在冻结集合内被取代的旧 raw 不能作依据。未知取代关系不靠目录扫描补猜。

`FrozenPage(path,content,sha256)` 是显式冻结的 wiki 依赖，全文哈希与磁盘核对。只在这些页和本批变更页中解析引用，不自动读取其他 Vault 内容，不从标题近似匹配来源。

`WikiSupportGate(checkpoint_root, gate_id, registry, model_config_hash, max_repairs=2, max_tokens=8192)` 使用显式私有 checkpoint 根；该根不能与 staging 相互包含。调用方须沿同一任务／批次保留 gate_id 和 checkpoint 根，禁止用新 ID／目录绕过恢复预算。configHash 必须不含凭据；它是摘要，不是凭据存放处。client 注入 `complete(system=..., user=..., max_tokens=...) -> str`，与现有 complete 客户端参数约定一致。本模块不启动服务，也不加载账号。

产品 caller 使用 `CodexWikiRunner.support_client(snapshot, runtime_root, *, task, registry, model, effort, source_proof=None, measure=None, skip_preflight=False, input_policy=None, max_application_input_bytes=INPUT_LIMIT)` 返回的 `WikiSupportClient`。其 `complete` 适配 Gate 接口，经 `check_support_json` 执行实际 typed CLI；`freeze_support` 冻结完整 raw、Registry、当前文件与批次，`parse_support` 校验外层绑定后把 checks 解包交给唯一 `parse_checks`。worker 显式传真实 source_proof 和 `APPLICATION_UTF8_POLICY`，验证完整应用输入／schema 的 UTF-8 字节及摘要，不伪称获得完整远端 token 计数或真实可用窗口。支持检查位于最终候选／体检后的发布之前；Gate 成功仍不是 accepted。

`gate.review(client)` 返回 `GateResult`，包含候选 hash、parent hash、具体诊断、严格核对结果、已用 repair 数和私有收据路径。至少一项 claim 且全部核对 supported 才能返回 `supported_candidate_not_published`。无变更论断为 `no_changed_claims`，它不是支持或发布结论。没有 `publish` 动作或 `publishable=True`。

## Registry 完整性与出处边界

Markdown-it 的 token.map 决定业务块和原文件行范围；不是先找有 citation 的行。覆盖段落、列表中的每个段落、blockquote 中的段落、完整表格（表头及所有行）、fence、缩进代码、HTML 块及非模板业务标题。每块为一个待核对 claim；模型必须核对块内全部子论断，表格不按一条引用就放行未引行。定位 ID 为路径＋章节／块类型／出现次序的程序摘要，原文件行范围只是辅助诊断。

新／改无出处业务块，包括来源摘要、列表、代码和任意 HTML 注释里的内容，都留下 `missing_citation`，不会静默漏掉。管理位置的部分引用诊断会随 claim 延后到 basis 核验，不直接豁免；raw/mixed 必须继续承担这些诊断。`[推测]` 标签随完整论断传入模型，不能豁免缺出处，也不能让 AI 推測变成用户原话。frontmatter 的 `当前判断`／`当前做法`、结果等业务字段独立登记；未知字段保守视为业务内容。模板章节名、页面身份标题、类型／日期／确认等结构字段不作为来源论断，仍依赖现有独立结构门禁；本模块不能替代它。

`标题`、`作者`、`原始文件`、`发布日期`、`发布时间` 不要求段落 citation，按明确的原始文件绑定冻结信封进行程序比较；标题／作者逐字，日期取原信封日期，带时区时间可按同一瞬间比较。没有发布时间只能匹配“未知”。原始文件指向变化时重核其他元数据；没有明确来源或值不符分别留下 `metadata_source_not_frozen`／`metadata_mismatch`。核验通过的元数据携带程序核验标记与信封进批量请求，不能被模型绕过失败诊断。

产品展示块只调用应用自有 `wiki_display.display_free_text` 验证后排除，并将原行置空以保留真实位置。绝不导入 staging/tools。普通 HTML comment 不作为排除规则。

自动章节仅限现有各页面类型的自动标题白名单，并且需可信调用方提供 `GeneratedSection(path,document_sha256,heading,content)`：完整文档 SHA 与完整章节 bytes 都匹配才排除。该证书必须来自调用方的可信 kb 执行证据，不能由模型从候选中自制。没有证书或证书不匹配，仍登记内容并要求出处；不因标题后缀／“自动生成”注释直接忽略。当前 worker `_managed_sections` 已运行可信 helper `managed describe-generated`，比对当前章节／文档字节，并识别固定语法的程序 check 日志；Gate 自身不运行 kb。

raw 引用必须准确指向 `raw/.../R-....md#^source-N`。程序从冻结 raw 中切出对应 anchor 前的原文范围，不把 code／HTML 中伪造的 anchor 作为编号。给模型当前完整论断、准确被引原文、完整相关 raw、信封与身份。全文上下文帮助解释条件与归属，不能为引用错配背书。

wiki 引用只能唯一解析精确路径／完整标题。无 anchor 仅接受单业务块依赖页；章节 anchor 也必须只定位一块；块 ID 可定位正文块／紧随其后的单独 ID。递归限定深度（默认 4），每层保存依赖论断全文和页面哈希，最终必须到具体 raw anchor。缺来源、多块歧义、不唯一、cycle、超过深度、综合页作依据均拒绝。依赖页正文不是原始事实。字面未改但引用本批变更 wiki 页的论断也重核。普通 Markdown 的非 raw 外部链接不作为支持证据。

## 批量核对协议与反馈

2026-10-08 审阅缺口修复：跳过字面未改的论断之前，先在本批变更页与显式 FrozenPage 集合内按相同 anchor／唯一性／深度规则解析完整支持链。任一层依赖页属于本批 changes，都重新登记该论断，例如 C 未改→B 冻结未改→A 本批变化。递归失败（含 cycle、unresolved、缺出处等）不能证明依据稳定，保留诊断并阻止支持结论；因此旧的未改但坏引用／无出处块也不会静默跳过。不扫描真实 Vault。新增 fake 意义测试让 A supported、仅 C 因条件扩大 unsupported，整个候选必须 source_support_failed；这证明 C 进入核对且失败控制有效，不证明模型理解能力。

一次 complete 请求包含全部当前 claims 和完整相关 raw；没有摘要替代全文或截断兜底。模型只判断来源是否支持，不进行外部真实性查证。系统要求核对否定、数值、条件、强度、归属及 cross_point，分别保留用户原话、第三方和 AI 推断。

Gate 内层 JSON 顶层只 `checks`。每个程序 claim_id 恰好一次；每项为 `claim_id/status/basis/reason/issues`，basis 仅 raw／program／mixed，status 为 supported／unsupported／uncertain，reason 非空。supported issues 空，其余至少一条；issue 只 `field/category/reason`，field 为 text／citations；category 为 negation／number／condition／strength／attribution／cross_point／unsupported／missing_context。仅不带 program_facts 的独立 legacy 调用允许省略 basis，按 raw 解析；产品 typed SUPPORT_SCHEMA 要求 basis，不能沿用旧四字段响应。typed 外层严格为 `contract/schema_revision/binding/registry_sha256/candidate_sha256/checks`，contract 为 `r14-typed-support-v1`、schema_revision 为 1。遗漏、重复、未知 ID、额外字段、无穷数、非法枚举等是技术失败，不是来源无价值。未通过的候选和具体诊断保留。

`program_facts` 是完整规范 JSON bytes，需同步 `program_facts_readback()` 严格相等，并绑定 candidate hash。worker `_program_facts` 经可信 `managed describe-state` 读取 `pages/pending/query_record`，另核验录制 terminal 与 final 摘要后追加 `completed_phases`。页面项只含 `path/sha256/type/confirmed/declared_topics`；pending 区分外部／自述路径；query_record 只记录存在与摘要；阶段项为 `phase/attempt/final_sha256`，新记录另含 before_spawn 持久化的 Taipei `started_at`，旧记录缺时间时不补造日期。`issue_counts/candidate_count/scan_observed_at` 来自该候选树、plan SHA 与阶段记录绑定的首次 protocol-scan 观测，跨日读回同一记录，不重算为今日计数；独立旧 API 仍接受无此三字段的四键事实。它们证明当前状态、已持久成功阶段与有时间的扫描观测，不证明模型“已读完”、历史检查值或独立 skip／具体更新动作。

除已程序核验的 provenance metadata 外，program/mixed 仅允许在 `wiki/log.md`、`wiki/体检报告.md` 和主题页 `概览` 的管理位置使用；位置合格不意味着文字自动有据。纯管理事实逐项核 program_facts，知识论断必须 raw，混合块必须 mixed 并同时核 raw 引文／语义。日志和体检报告不得递归成为知识来源；program 不得替知识消除引用错配。

generation／health 初始提示传入宿主 `host_context` 的活动日期与当次计数，结构日期必须合法 YYYY-MM-DD；support 按绑定事实核验。当前页面存在及可检查 meta 描述走 program；当前候选知识关系可按实际全文与有效 raw 依据链独立判断，不要求先有 semantic-audit 事件，但仍须 raw/mixed 与准确 raw anchor。该判断不是历史执行证明，未给全库全文不能声称全库无矛盾或全面审查。

异常只固定代码，不带路径、raw、响应正文或客户端异常。模型理由只存在私有 `GateResult`、反馈及私有记录中；调用方不得把它们不加筛选写产品日志。unsupported 和 uncertain 都阻止支持结论。

## 持久预算与修复边界

命名空间按稳定 gate_id，首次 binding 固定冻结 raw 路径／ID／SHA、before 文档路径／SHA、冻结依赖页、深度以及合同／抽取／恢复版本；配置和 max_repairs/max_tokens 首次固定，后续不匹配拒绝。候选 hash 不参与预算 namespace，不因当前失败 claim 改变预算。

私有 task 目录 0700，记录与锁 0600，非阻塞进程锁覆盖请求窗口，原子记录 fsync 文件及父目录。候选全文、请求身份及已占预算在 complete 调用之前保存；收到响应立即另存原响应。重启只读 state 指定的记录并验证内容摘要，不扫孤儿文件猜最新；缺响应返回 interrupted，不自动重发。已有响应每次严格重验，不信缓存的成功布尔。损坏／symlink／身份不匹配不能覆盖成新预算。

`reserve_repair()` 必须在现有 runner 的外部修复调用之前执行，先持久占用预算，返回 `RepairReservation` 和完整父候选、失败字段、诊断及约束。默认最多两次额外 repair（0–10 可配置）；达到上限返回 `feedback.status=budget_exhausted` 和空 token，没有再调用授权。已有未消费 reservation 重启不能重新领取，因无法判定请求是否已发；调用方应保留原 reservation 和已收到候选，若没有可靠结果则报告中断。本模块不自行创建第二 WikiAgent，也不执行反馈里的任何指令。

修复后再次 `build_registry(..., parent_registry=old, claim_mapping=...)`，映射必须是原 claim 顺序的 `ClaimMapping(original_claim_id,path,position)`。再 `new_gate.review(client,reservation=reserved)`。要求原 IDs／数量／顺序、路径、章节和块类型一致，不用行号重新猜对应；补引用或块内换行不改变 ID。缺映射、删旧块、增块、交换旧块文本或改成功字段均拒绝；失败范围外的文档 bytes 也必须精确保留。允许字段仅来自诊断，不允许通过撤回失败 claim 过关。引用-only 修复按引用出现位置比较，不全局删除地址子串；保留显示标签、无标签 wiki 名称及普通正文，不能制造文字未改的假等价（`d1ac29f`）。

已有候选跨日恢复时，`cached_generated` 只读该 Gate 最后候选的 GeneratedSection 证书；caller 先核 cached check／候选树，再重建 Registry 严格比较 binding hash、candidate hash 和 documents，正常 Gate review 仍重验状态。它不为新模型 bytes 签发证书；未消费 repair 的父候选不能冒作已检查子候选（`a096bdc`）。

同候选同失败立即 `no_improvement`，不重复模型调用。候选有变化但失败字段位置没有严格缩小也 `no_improvement`；唯一阶段例外是父候选仅有硬边界失败、当前硬错误已清除且首次真正完成语义检查，此时可使用原预算剩余额度，不把进入下一检查阶段误判为无进展。改理由或换失败 claim 不能刷预算。技术故障没有自动 repair。成功仍只 supported_candidate_not_published。

## 不适用、验证与后续

2026-10-08 私有 checkpoint 修复：显式 checkpoint_root 与 task namespace 均须是当前 UID 所有的 0700 目录，已有公共权限目录直接拒绝，不自动 chmod。逐级检查 symlink ancestors。私有记录与锁须是当前 UID 所有、0600、nlink=1 的常规文件；读取前 lstat、打开后 fstat 及读取后 fstat 均检查，并比较打开文件与路径 inode。锁以 O_NOFOLLOW／O_NONBLOCK 打开，仅在该已验证且持续持有的 fd 上执行非阻塞 POSIX flock，整个请求期间不再调用共享 file_lock 按路径重新 open；RLock 非阻塞包围同一窗口，错误不会降为无锁执行。保留 write_record 原子替换、文件 fsync 和父目录 fsync，写后再验证私有记录。此实现以 POSIX current UID／flock 为边界，Windows 适配尚未验证；不声称防住同 UID 恶意进程在检查后替换目录、锁 inode 或写入全过程的竞态。

历史初稿曾只完成代码／回归设计及 AST 检查，尚未运行新增 pytest；后来 95 pass 的独立修复回归见开头历史记录。该先后状态不再描述当前未执行事项，也不以历史数量替代当前 typed 接线和 program basis 的验收。本次文档维护不运行重复测试。

图片、音视频原生引用、图片 anchor 或夹带媒体的论断明确 `unsupported_kind`；本合同不理解图片／波形／视频事实，也不借无 changed claims 宣称其已验证。删除、非 Markdown、外部事实验证、用户确认和正式 Vault 写入不属于 Gate API；产品 worker 的 publish 与正式 accepted 是后续独立步骤。真实产品全部输入及模型语义效果仍需独立验收，不能据文本合同或接线宣布整个 R14 完成。

`tests/v1/test_wiki_support.py` 使用 tmp_path 合成文件与 fake complete，覆盖全块／摘要遗漏、范围／身份／依赖、严格协议、稳定修复映射、成功字段保护、预算／重启／崩溃收据、symlink／SHA／UTF-8／并发锁。`test_wiki_support_runner.py`、`test_wiki_worker_typed.py` 等另覆盖 typed 传输与实际 worker 控制路径；fake 判决只验证合同和控制链，不证明真实模型语义或正式发布。本次未重跑这些测试，未接触真实模型、凭据、正式 DB/Vault、部署、安装或 publish。

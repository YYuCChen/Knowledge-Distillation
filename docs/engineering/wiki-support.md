# R14 raw→wiki 独立来源支持合同

2026-10-08，Asia/Taipei。实现为 `src/knowledge_distiller/v1/wiki_support.py`，尚未生产接线或主控语义验收；旧候选已有 73 pass fake 测试证据，本次审阅修复的新测试尚未执行。它不调用旧 SQL 知识主链，不修改旧候选／支持／恢复模块、worker、runner、publish、kit、UI 或数据库。

## 输入与 API

`DocumentChange(path,before,before_sha256,after,after_sha256)` 保存完整 bytes 和明确 SHA256。新建的 before 两值均为 None；删除不属于本合同。调用方在独立 staging 完成候选后调用 `build_registry(staging_root, changes, raws, pages=(), generated=(), max_depth=4)`。所有 after、raw 及依赖页都必须与 staging 常规文件逐字一致；before 必须为冻结基线 bytes，调用方负责基线来源。程序检查 UTF-8、摘要、路径及符号链接，绝不写 raw。

`FrozenRaw(path,stable_id,content,sha256)` 给出完整 raw，编号与文件名／信封相同，身份只能第三方／本人／本人附言，路径身份一致，不创建或修改正式 ID。缺作者保存为“未知”，不猜作者。重复 raw ID、重复 source anchor、已在冻结集合内被取代的旧 raw 不能作依据。未知取代关系不靠目录扫描补猜。

`FrozenPage(path,content,sha256)` 是显式冻结的 wiki 依赖，全文哈希与磁盘核对。只在这些页和本批变更页中解析引用，不自动读取其他 Vault 内容，不从标题近似匹配来源。

`WikiSupportGate(checkpoint_root, gate_id, registry, model_config_hash, max_repairs=2, max_tokens=8192)` 使用显式私有 checkpoint 根；该根不能与 staging 相互包含。调用方须沿同一任务／批次保留 gate_id 和 checkpoint 根，禁止用新 ID／目录绕过恢复预算。configHash 必须不含凭据；它是摘要，不是凭据存放处。client 注入 `complete(system=..., user=..., max_tokens=...) -> str`，与现有 complete 客户端参数约定一致。本模块不启动服务，也不加载账号。

`gate.review(client)` 返回 `GateResult`，包含候选 hash、parent hash、具体诊断、严格核对结果、已用 repair 数和私有收据路径。至少一项 claim 且全部核对 supported 才能返回 `supported_candidate_not_published`。无变更论断为 `no_changed_claims`，它不是支持或发布结论。没有 `publish` 动作或 `publishable=True`。

## Registry 完整性与出处边界

Markdown-it 的 token.map 决定业务块和原文件行范围；不是先找有 citation 的行。覆盖段落、列表中的每个段落、blockquote 中的段落、完整表格（表头及所有行）、fence、缩进代码、HTML 块及非模板业务标题。每块为一个待核对 claim；模型必须核对块内全部子论断，表格不按一条引用就放行未引行。定位 ID 为路径＋章节／块类型／出现次序的程序摘要，原文件行范围只是辅助诊断。

新／改无出处业务块，包括来源摘要、列表、代码和任意 HTML 注释里的内容，都留下 `missing_citation`，不会静默漏掉。`[推测]` 标签随完整论断传入模型，不能豁免缺出处，也不能让 AI 推測变成用户原话。frontmatter 的 `当前判断`／`当前做法`、结果等业务字段独立登记；未知字段保守视为业务内容。模板章节名、页面身份标题、类型／日期／确认等结构字段不作为来源论断，仍依赖现有独立结构门禁；本模块不能替代它。

`标题`、`作者`、`原始文件`、`发布日期`、`发布时间` 不要求段落 citation，按明确的原始文件绑定冻结信封进行程序比较；标题／作者逐字，日期取原信封日期，带时区时间可按同一瞬间比较。没有发布时间只能匹配“未知”。原始文件指向变化时重核其他元数据；没有明确来源或值不符分别留下 `metadata_source_not_frozen`／`metadata_mismatch`。核验通过的元数据携带程序核验标记与信封进批量请求，不能被模型绕过失败诊断。

产品展示块只调用应用自有 `wiki_display.display_free_text` 验证后排除，并将原行置空以保留真实位置。绝不导入 staging/tools。普通 HTML comment 不作为排除规则。

自动章节仅限现有各页面类型的自动标题白名单，并且需可信调用方提供 `GeneratedSection(path,document_sha256,heading,content)`：完整文档 SHA 与完整章节 bytes 都匹配才排除。该证书必须来自调用方的可信 kb 执行证据，不能由模型从候选中自制。没有证书或证书不匹配，仍登记内容并要求出处；不因标题后缀／“自动生成”注释直接忽略。此独立模块不运行 kb，也尚未证明后续生产调用方的证书生成适配。

raw 引用必须准确指向 `raw/.../R-....md#^source-N`。程序从冻结 raw 中切出对应 anchor 前的原文范围，不把 code／HTML 中伪造的 anchor 作为编号。给模型当前完整论断、准确被引原文、完整相关 raw、信封与身份。全文上下文帮助解释条件与归属，不能为引用错配背书。

wiki 引用只能唯一解析精确路径／完整标题。无 anchor 仅接受单业务块依赖页；章节 anchor 也必须只定位一块；块 ID 可定位正文块／紧随其后的单独 ID。递归限定深度（默认 4），每层保存依赖论断全文和页面哈希，最终必须到具体 raw anchor。缺来源、多块歧义、不唯一、cycle、超过深度、综合页作依据均拒绝。依赖页正文不是原始事实。字面未改但引用本批变更 wiki 页的论断也重核。普通 Markdown 的非 raw 外部链接不作为支持证据。

## 批量核对协议与反馈

2026-10-08 审阅缺口修复：跳过字面未改的论断之前，先在本批变更页与显式 FrozenPage 集合内按相同 anchor／唯一性／深度规则解析完整支持链。任一层依赖页属于本批 changes，都重新登记该论断，例如 C 未改→B 冻结未改→A 本批变化。递归失败（含 cycle、unresolved、缺出处等）不能证明依据稳定，保留诊断并阻止支持结论；因此旧的未改但坏引用／无出处块也不会静默跳过。不扫描真实 Vault。新增 fake 意义测试让 A supported、仅 C 因条件扩大 unsupported，整个候选必须 source_support_failed；这证明 C 进入核对且失败控制有效，不证明模型理解能力。

一次 complete 请求包含全部当前 claims 和完整相关 raw；没有摘要替代全文或截断兜底。模型只判断来源是否支持，不进行外部真实性查证。系统要求核对否定、数值、条件、强度、归属及 cross_point，分别保留用户原话、第三方和 AI 推断。

严格 JSON 顶层只 `checks`。每个程序 claim_id 恰好一次；每项只 `claim_id/status/reason/issues`，status 为 supported／unsupported／uncertain，reason 非空。supported issues 空，其余至少一条；issue 只 `field/category/reason`，field 为 text／citations；category 为 negation／number／condition／strength／attribution／cross_point／unsupported／missing_context。遗漏、重复、未知 ID、额外字段、无穷数、非法枚举等是 `technical_failure`，不是来源无价值。未通过的候选和具体诊断保留。

异常只固定代码，不带路径、raw、响应正文或客户端异常。模型理由只存在私有 `GateResult`、反馈及私有记录中；调用方不得把它们不加筛选写产品日志。unsupported 和 uncertain 都阻止支持结论。

## 持久预算与修复边界

命名空间按稳定 gate_id，首次 binding 固定冻结 raw 路径／ID／SHA、before 文档路径／SHA、冻结依赖页、深度以及合同／抽取／恢复版本；配置和 max_repairs/max_tokens 首次固定，后续不匹配拒绝。候选 hash 不参与预算 namespace，不因当前失败 claim 改变预算。

私有 task 目录 0700，记录与锁 0600，非阻塞进程锁覆盖请求窗口，原子记录 fsync 文件及父目录。候选全文、请求身份及已占预算在 complete 调用之前保存；收到响应立即另存原响应。重启只读 state 指定的记录并验证内容摘要，不扫孤儿文件猜最新；缺响应返回 interrupted，不自动重发。已有响应每次严格重验，不信缓存的成功布尔。损坏／symlink／身份不匹配不能覆盖成新预算。

`reserve_repair()` 必须在现有 runner 的外部修复调用之前执行，先持久占用预算，返回 `RepairReservation` 和完整父候选、失败字段、诊断及约束。默认最多两次额外 repair（0–10 可配置）；达到上限返回 `feedback.status=budget_exhausted` 和空 token，没有再调用授权。已有未消费 reservation 重启不能重新领取，因无法判定请求是否已发；调用方应保留原 reservation 和已收到候选，若没有可靠结果则报告中断。本模块不自行创建第二 WikiAgent，也不执行反馈里的任何指令。

修复后再次 `build_registry(..., parent_registry=old, claim_mapping=...)`，映射必须是原 claim 顺序的 `ClaimMapping(original_claim_id,path,position)`。再 `new_gate.review(client,reservation=reserved)`。要求原 IDs／数量／顺序、路径、章节和块类型一致，不用行号重新猜对应；补引用或块内换行不改变 ID。缺映射、删旧块、增块、交换旧块文本或改成功字段均拒绝；失败范围外的文档 bytes 也必须精确保留。允许字段仅来自诊断，不允许通过撤回失败 claim 过关。引用-only 修复不得改其余论断文字，显示标签仍作为文字保留。

同候选同失败立即 `no_improvement`，不重复模型调用。候选有变化但失败字段位置没有严格缩小也 `no_improvement`；改理由或换失败 claim 不能刷预算。技术故障没有自动 repair。成功仍只 supported_candidate_not_published。

## 不适用、验证与后续

2026-10-08 私有 checkpoint 修复：显式 checkpoint_root 与 task namespace 均须是当前 UID 所有的 0700 目录，已有公共权限目录直接拒绝，不自动 chmod。逐级检查 symlink ancestors。私有记录与锁须是当前 UID 所有、0600、nlink=1 的常规文件；读取前 lstat、打开后 fstat 及读取后 fstat 均检查，并比较打开文件与路径 inode。锁以 O_NOFOLLOW／O_NONBLOCK 打开，仅在该已验证且持续持有的 fd 上执行非阻塞 POSIX flock，整个请求期间不再调用共享 file_lock 按路径重新 open；RLock 非阻塞包围同一窗口，错误不会降为无锁执行。保留 write_record 原子替换、文件 fsync 和父目录 fsync，写后再验证私有记录。此实现以 POSIX current UID／flock 为边界，Windows 适配尚未验证；不声称防住同 UID 恶意进程在检查后替换目录、锁 inode 或写入全过程的竞态。

本次修复只完成代码／回归设计及 AST 语法检查，不运行 pytest；此前 73 pass 属于旧候选的 fake 控制链证据，不作为这两项语义修复的验收。新增回归仍由 Luna/max 在显式可丢弃目录执行，主控据实际 diff 和证据验收。

图片、音视频原生引用、图片 anchor 或夹带媒体的论断明确 `unsupported_kind`；本批不理解图片／波形／视频事实，也不借无 changed claims 宣称其已验证。删除、非 Markdown、外部事实验证、用户确认、正式 Vault 写入和 publish 不属于本合同。未来真实产品全部输入适配尚需独立证明，不能据本批文本合同宣布整个 R14 完成。

新测试 `tests/v1/test_wiki_support.py` 只用 pytest tmp_path 合成文件与 fake complete，覆盖全块／摘要遗漏、范围／身份／依赖、严格协议、稳定修复映射、成功字段保护、预算／重启／崩溃收据、symlink／SHA／UTF-8／并发锁。fake 判决仅验证控制链路。Sol 已作 AST 语法解析，尚未运行这些测试；主控交 Luna/max 在 CPython 3.11.16、显式独立可丢弃目录运行限定节点并保留 JUnit，再据实际 diff／结果验收。无真实模型、凭据、正式 DB/Vault、部署、安装或 publish 接触。

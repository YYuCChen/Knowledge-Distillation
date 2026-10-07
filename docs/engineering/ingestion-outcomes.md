# R01/R08 隔离采集与逐原件结果合同

2026-10-08，Asia/Taipei。第一步编码候选，**没有产品接线；首测失败及定向复验见后文**。本合同遵循父工程现行宪法与本轮主控确定性决策；不改变模板、static、路由、可见状态、app装配、worker、数据库主迁移或kit。

## 权威边界

采集唯一成功事实是 `raw_verified`；它不意味着有知识，也不创建wiki任务。`processed_with_knowledge` / `processed_no_knowledge` 只属于用户独立启动的wiki整理。后者必须核验完整冻结原件及具体原因；技术失败、缺上下文、空点、日志出现路径、字数短均不是正常无知识。

schema **24登记保留**，实际 `database.SCHEMA_VERSION` 不改。本阶段仅两模块各自显式 `initialize()` 私有候选数据库，不增加 ingestion_schema.py，不自动调用系统数据库初始化，不把候选表写进App数据库。升级迁移和旧活动任务续跑未接线、未验收。

## ingestion.py 接口

`Ingestion(store, candidate_database)`：显式传入已初始化的隔离Store和另一私有数据库绝对路径。父目录必须已存在。`initialize()` 创建该模块自己的两张表及不可变触发器；已有其他业务表或权限过宽的文件拒绝。新文件0600，SQLite FULL synchronous、30秒busy timeout，每笔事件/留存使用BEGIN IMMEDIATE，显式关闭连接。初始化不扫历史，不补写任何旧raw。

- `material(material_id, vault, item_id=None) -> RawReceipt`：读取实际source_fact和原生版本/snapshot_key；明确来源未就绪则拒绝。feishu_voice必须走capture。传item时核对材料关联与确认状态；附属capture必须是已确认第三方。复用RawLedger.ensure_material/material_hints以及write，不derive、不establish_knowledge、不调用V1 publisher、不改item state。
- `capture(capture_id, vault) -> RawReceipt`：复用Captures的身份、邻接、目标、source fact及已分配capture raw；复用其render核对当前source正文和音频material ID，source_version绑定身份事件、source fact与正文hash。不批量write_ready、不mark_succeeded、不release_audio。本人/附言身份仍是capture，不能另写第三方material raw。
- `verify_record(ledger, record, vault, source_version=...)`：重新安全读取文件；确切bytes与ledger字节相等、SHA一致、稳定ID/path/身份/格式版本1、material/capture subject回溯一致。材料额外核对source_fact ID。附件与保留的原图逐字节和hash一致。已written_at也重读，缺失/不同/符号链接拒绝。
- `events("material:<id>"或"capture:<id>")`：只读私有事件；它不是产品状态投影。

RawReceipt包含subject kind/ID、raw ID/path/identity/format_version、byte_count/hash、准确source_version和附件path/hash。源内容版本、格式版本与字节hash各司其职，不更改旧编号/原件。

安全解析使用SafeLoader，仅数据，没有对象构造权限；重复映射键拒绝。现有raw.parse_envelope只返回有限字符串字段，无法核验typed版本/应用记录，本模块的解析适配不修改raw模块。

### 留存和崩溃边界

`ingest_retained`先以同raw ID/hash绑定保存正文及附件确切字节，再调用RawLedger.write。保存成功后发raw_pending；成功读回后追加raw_verified。事件去重键绑定subject/kind/binding/合同/具体诊断；原事件和字节禁止update/delete。同一个错误重放幂等；这不是每次点击的计费或attempt账本。

分配后未写盘、附件后中断、正文写成但事件尚未提交均可再次显式调用；现有Ledger保证同号/no-clobber，事件事务跨进程去重。不存在自动重试/后台循环/TTL或清理函数。本阶段不调用Store.mark_failed，不设旧72h TTL，不释放图像/录音、不删除资料。私有字节保留到后续明确生命周期决策。

**精确capture接口缺口（已定位，未绕过）：** Captures.ready要求audio item先succeeded，write_ready还会批量写并释放原音。尚未分配capture raw时返回 `capture_precise_writer_required`；不能冒充raw成功。建议主控串行锁captures.py，抽取该模块内的精确 `ensure_ready(capture_id)`，保留现有render/预留号/身份/邻接/附言逻辑，以source_ready为门槛、分配而不写其他capture且不释放音频。由主控决定共享实现，不在本worker另复制。

**关联核验：** raw_id_of_message会把预留capture编号也视为settled。本候选要求每个邻接/附言目标都有真实ledger record并在指定vault可读回；预留号、unsettled、缺目标不成功。已分配capture信封邻接必须与明确列表一致，身份/附言对象必须匹配最新决策，变化留待取代，不回改raw。

2026-10-08 定向修正：在原Captures单ID投影之前，只读检查完整message/capture/全部feishu_parts/source事实及全局未被supersede的raw heads。有效part缺item/source/current raw、仍待确认、失败/撤下或receipt未accepted均pending；不以failed=settled代替保留证明。已写的每份都读回，多个不同current raw明确ambiguous，不选ids[0]。无item且明确error的无效part不伪造raw；若没有任何真实原件仍pending。本人audio复用capture raw与其material关联，不另写第三方。邻接逐条先做完整检查再核对原Captures.adjacency投影；非capture的feishu_parts材料也在ensure_material之前检查所属消息/邻接及material_hints一致性。一个item关联多个消息时拒绝任选LIMIT 1。目标取最新current head，原注释绑定旧target时拒绝，不回改候选或历史原件。

当前单target/单邻接ID合同不能表达一消息多个对象。本步只拒绝歧义，不造多对象writer/allocate/render。若要完整表达，需要主控串行调整Captures消息投影为全对象及complete/ambiguous状态，并同步邻接/附言信封和相关UI审阅；本worker未接这项共享差异。只读关系检查不是跨采集/取代写入的全局事务，生产接线仍须采用主控现有串行/冻结边界。

第二次最窄修正：read_regular在任何文件访问前拒绝含冒号的relative，包括C:/absolute与C:drive-relative；合法raw路径不需冒号。_message_raw读取capture完整message_type、当前item绑定及最新完整身份事件；最新third_party但仍有未被取代的本人/附言capture head一律message_raw_pending，不能借旧material_id当当前第三方来源。本人/附言复用现Captures.render逐字核对完整body及信封的身份判定、目标、邻接、订正、存疑、渠道、作者、来源时间、应用记录和识别来源；不比较允许取代后变化的收录时间及应用构建版本，不复制writer。附言目标重新取当前消息原件，旧target或正文绑定变化均pending，不生成新raw。

依赖核验只允许一层：依赖消息若自身是附言或还有邻接，保守pending；包含循环的关系也拒绝，不实现通用递归/多对象框架。这可能拒绝有效的嵌套关系，待主控另行决定完整接口及冻结边界。有效无依赖self/audio与单target附言保持合同；目标原件自身取代后旧附言仍拒绝。新增两种drive路径、两种旧本人身份改第三方、同身份更换目标、正文投影/音频新材料绑定不一致及自环合成回归。

Luna唯一首测报告 `/tmp/kd-v3-r01-tests-20261008.md`：固定3.11.16、fresh root `/private/tmp/kd-v3-r01-tests-20261008.ILDalG`，78 collected/77 passed/1 failed；JUnit SHA256 `572c14d6ee4f9ac4043f5d0dfd3bf6e1207a9bb4cfa5ff251a938de701e026ce`。失败的文字case直接UPDATE captures.text触发 `captures is immutable`，是测试夹具违背生产不可变合同，不能称通过。现仅修夹具：同capture/同ID及原信封不变，controlled render利用原Captures.render生成不同正文，只替换返回投影的body；权威capture.text不变并另有断言，原raw字节保留断言与message_raw_pending拒绝断言保留。它证明完整正文投影不一致的诊断保护，不证明原文可变、平台重传或合法新source版本。audio仍用新material绑定更正，不更新SourceFact。没有删除trigger、改变生产代码/immutability或弱化拒绝条件；本修正仅AST/读回，尚未pytest复验，须主控审diff后授权Luna必要复验。

文件读回修正：打开后、任何read前，held FD必须是regular且与初始inode/key一致；key核对dev/ino/size/mtime/ctime/mode/nlink（mode含类型）。named文件与父目录类型/身份在前后核对；目录不比mtime/ctime，避免正常邻居文件写入造成无意义拒绝。这提供适度保守的替换检测，**不担保同UID攻击者在syscall间隙瞬时替换并还原**，不宣称通用文件安全框架。候选raw_pending/readback诊断仅固定码；未知错误压成raw_write_failed/raw_readback_failed，不存任意异常str或正文作原因。

**保留限制：** 私有候选有未核验字节副本，不是生产媒体生命周期gate。RawLedger.write会先登记written_at，现有媒体释放规则不读取本候选raw_verified；必须在第二步一起改该生产门槛，否则不能声称原始应用媒体仍安全保留。若外部旧TTL/sweep仍删除Store.source_media，现有RawLedger不能从候选副本自动恢复读取；本模块不会写回共享源表来绕过所有权。第二步主控必须锁定production TTL与精确Ledger恢复接口，再声称生产字节受到完整保护。voice尚未分配raw时也不自动拷贝/接管audio生命周期。

## wiki_outcomes.py 接口

`WikiOutcomes(candidate_database)` + 显式 `initialize()`：仅独立 `wiki_outcome_receipts` 和不可变触发器。不扫旧wiki，不把旧日志字符串推断成新版成功，不连接默认App路径。

`frozen_context(task, batch_no, contents)`：接受真实WikiTask/FrozenRaw全部字段；用现有wiki_tasks._boundary对完整task冻结清单验hash，再要求本批contents恰好覆盖每raw ID、原件完整bytes/byte_count/hash/ID/身份/格式版本一致。上下文完整供checker，不截断，不用摘要替代原件。

定向修正核对真实算法：_boundary仅哈希传入批次的path/ID/identity/byte_count/hash，**不包含ordinal或batch_no**。按已知batch分组会遗漏落在不存在batch的raw；因此先验完整task.raw的唯一ID/path、原ordinal严格1..N且原顺序不改、batch存在且有序、batch编号1..batch_count、各item_count与raw_count准确、所有raw元数据合法，再做现有hash和本批完整bytes核验。不能仅核当前batch。原合成任务工厂的ordinal起点同步改为现行Store实际的1。

`Outcome`：raw ID/hash、合同 `r08-wiki-outcomes-v1`、task boundary hash、正常status、reason_code/具体reason、将发布的wiki文档path/hash。每批必须每个raw恰一；少、重复、未知、旧合同、错误hash、失败/unknown伪装正常都拒绝。

`validate(task,batch_no,contents,outcomes,checker=...,support_gate=...,support_client=...) -> receipt_id`：

1. 确定性边界/逐项覆盖检查。
2. 无知识原因只允许non_substantive、no_distinct_claim、support_only；空理由、“太短”/“无知识”不能提交，未保留附件不能进入正常无知识。
3. 每份无知识必须独立调用checker.review，提供完整FrozenRaw、full_raw bytes、具体Outcome与本批全部完整原件关系上下文；checker需返回NoKnowledgeReview，状态verified、source_complete=True、具体验证理由，并覆盖definition/method/reference_lead/relations四维。unknown/technical_failed/unsupported/不完整/超时全拒绝，无正常终态。
4. 有知识或support_only必须提供真实R14 WikiSupportGate实例；其registry冻结raw必须与本批ID/hash/bytes精确一致，每份声称有知识/贡献支持的raw必须出现在所绑定文档的claim evidence。直接调用R14 gate.review并复用其原receipt/预算；只有supported_candidate_not_published且无diagnostics才通过。文档hash必须与R14 changes一致，拒绝空点或拿别份支持偷换本份。
5. 只存validated候选回执，记录R14 receipt字节hash；不publish、不accepted、不更新任务成功状态。各原件出处、区间、作者/AI归属和转载独立性继续由R14 registry/support合同与生成链负责，本模块不复制或另造关系推理算法。

**语义限制：** 当前只有checker Protocol和fakechecker合成测试，没有真实NoKnowledgeChecker模型适配器，也未实测模型判断完整性/短价值/独立性。source_complete是独立checker的候选判定，不是事实证明。非关键不确定内容保留在完整raw中；是否足以判无知识由checker判断，不能确认则unknown。真实部署前必须提供完整性/结构证据与check实现，并验其语义有效性。

定向修正：独立NoKnowledgeReview.reason也必须字符串、非空且不是单独“太短/无知识”等泛词（含常见标点）；status严格字符串verified，source_complete严格bool True，considered严格tuple且四个成员全部字符串、恰覆盖四维，无重复。非法类型返回固定no_knowledge_unknown，不以AttributeError/TypeError或fake试验冒充语义判断。

`plan_explicit_groups(raws,contents,groups,budget=...,measure=...,target_size=5)`：纯隔离规划；显式同题14份完整同批，20份示例为14/5/1，14不是总任务上限。group不重叠/不重复/不引用未知raw。measure必须由完整prompt的实际tokenizer提供成本；任何完整批超过budget拒绝，不截断、不自动关系批、不要求强制综合。返回计划未接WikiTaskStore原plan_batches；接入需主控锁wiki_tasks并使task boundary包含所选同题冻结关系。

### accepted边界

`accept(receipt_id, task_store, snapshot, journal, lock)`没有published=True参数，不能用flag自行接受：

- 从私有validated回执重算ID/合同；通过实际WikiTaskStore重新读task/batch/frozen raw，必须同task boundary且现有批读回已succeeded。
- journal路径必须是该snapshot.control的publish-<batch>；复用现有wiki_publish._load_journal，要求COMMITTED、非空、逐file verified、唯一路径和Outcome所绑定document hash一致。
- 持真实VaultWriteLock调用既有verify_recovered_publish，核验发布后文件与原始输入/kit/受保护边界；所有task.raw再次读回bytes/hash，R14源支持receipt未变，journal前后字节未变。
- 所有核验完成才以单独事务追加accepted。相同receipt幂等；同task/batch另一不同receipt不能二次accepted，跨进程事务防冲突。若commit之后私有事务中断，保留snapshot+journal再次显式accept，不重跑生成、不覆盖用户后改。

复用的publish恢复接口不会自动执行知识生成、修复、回滚或正式Vault写入；本阶段accept只是核验调用，合成测试的实际publish仅在显式临时Vault里执行。当前不增加kb pending协议、不产生最终可重建的Vault投影；私有validated/accepted只是候选过程证据，**不是R01/R08产品已完成**。将来理由/关系在现有wiki/log或来源页的可重建投影须先通过具体UI输出审阅。

## 测试与静态验收

新测试只用显式tmp_path/子进程合成参数：RawLedger真实写入与附件读回、written_at后丢失/改写/符号链接、ID/格式/身份、不可用恢复、精确capture缺口、保留音频、预留目标/邻接、跨进程与正文后崩溃；逐raw边界、完整上下文独立检查、短定义/关键缺口、R14真实gate+fake支持响应、14显式组/超预算/20不限量、12+2覆盖、真实publisher合成journal/readback与用户后改拒绝、私有事务中断/进程去重。

本worker仅做AST/compile静态检查，不运行pytest、不导入或启动业务、不访问正式数据。机械测试由Luna/max后续固定CPython **3.11.16**、freshroot执行；fakechecker和FakeClient仅证明合同/控制流，不证明真实知识语义。

定向新增测试：同消息两个有效part均写→ambiguous、只写首项/另项working或failed→pending；首项raw导致旧adjacency吞unsettled的实证；非capture材料在邻接完整前不得分配raw；单target成功及目标supersession后旧annotation拒绝；单part多个current heads；open时换FIFO不得read，read中mode/nlink/父目录替换；完整task的孤儿raw/跨批重复ID/ordinal/batch/count错误；独立check的泛词reason及错误typed结果；外部异常正文不得进入raw_pending诊断。运行证据以本文件记录的Luna首测及失败限制为准，修正夹具未复验。

后续限定命令（先由Luna确认自身JSONL配置，不使用父ID）：

```sh
cd '/Users/chen./Documents/知识蒸馏器/V3.0开发/source'
task_test_root=$(mktemp -d /tmp/kd-v3-r01-step1-20261008.XXXXXX)
PYTHONPATH='/Users/chen./Documents/知识蒸馏器/V3.0开发/source/src' TZ=Asia/Taipei \
  '/Users/chen./Documents/知识蒸馏器/V1.3开发-20260914/source/.venv/bin/python' \
  -m pytest tests/v1/test_ingestion.py tests/v1/test_wiki_outcomes.py \
  --basetemp "$task_test_root/pytest" --junitxml "$task_test_root/results.xml"
```

已只读执行该解释器 `-V`，确为Python 3.11.16；未安装依赖，未运行上述命令。root每轮新建，不复用旧证据。解释器在旧source但PYTHONPATH仅指新source；测试重用新source既有test_raw/test_wiki_worker/test_wiki_support的合成helper，不复制正式DB/Vault。不改HOME/CODEX_HOME，不用真实runner/网络/模型。缺依赖或helper接口变化原样报告，不自动安装或删断言。

## 未接线与共享文件差异登记

目前唯一业务新增文件是ingestion.py/wiki_outcomes.py；不改raw.py/Captures/Store/pipeline/app/collections/WikiTaskStore/runner/worker/database/kit。需要串行锁的下一步仅登记：精确capture ensure方法、未核验媒体TTL门槛、显式组冻结进task、支持gate在既有worker发布前的位置、正式accepted与恢复登记、最终Vault重建/旧skip兼容。全部生产可见状态、文字、来源路径/链接、飞书回执与结果展示仍待具体批准；无隐藏feature flag。

正式发布授权继承最新用户裁决，由主控验收/发布；本worker不push/install，不真实数据迁移，不启动模型或应用。历史记录保留只读；旧任务缺新版合同不自动升级或重跑。当前无提交，完整实际新增diff与SHA随handoff记录。

## 2026-10-08 主控审阅与必要复验

主控实际读取两个完整业务模块、测试、两轮定向修复与最后夹具差异；Gibbs 自身 JSONL 核验为 gpt-5.6-luna/max。首次仅两测试文件运行，固定 CPython3.11.16、env -i、fresh `/private/tmp/kd-v3-r01-tests-20261008.ILDalG`，78 collected、77 passed、1 failed、0 error/skipped，exit1；JUnit SHA256 `572c14d6ee4f9ac4043f5d0dfd3bf6e1207a9bb4cfa5ff251a938de701e026ce`。失败栈和违背 captures 不可变合同的原夹具留在报告，不报告首稿全通过。

业务代码没有为测试改变，仅修受控 render 投影不一致夹具。随后在新根 `/private/tmp/kd-v3-r01-fixture-fix-tests-20261008.3ttFkm`，相同隔离条件下仅运行 `tests/v1/test_ingestion.py::test_message_capture_rejects_changed_source_body`，两个参数均通过、0 failed/error/skipped、exit0。没有重跑其余76项。JUnit SHA256 `9e4cc59d2a308daf63e82787ce248cde51a741f2de5e66649c525919e5de80fd`，主控独立读回 XML 和两节点。首测已通过 True，本次与它重叠；两批覆盖78个不同通过节点，不能加成79，也不是一次新的完整78项运行。

两次业务源码均保持：ingestion SHA256 `a83a9686ee4d69b78735746e20eaa2cbd260ea4b30ebe86f7e45572e5536c853`，wiki_outcomes `7f393d01169b942bab2ac99c3f7bcd14d6cd09dbbfe3fb42d113e2477018c7ac`。修后 test_ingestion `7be6d0a2974b4ea470c87e7ca1fd9b756f4b1a787b7831ff827f588324e31bab`，test_wiki_outcomes `e4c440334eef4fe6e52fd67a82635c07afd1817b9a7940fb0e362a5045103879`。所有材料、RawLedger、publisher、子进程与崩溃点仅独立临时合成目录；未接正式 DB/Vault/profile/凭据、网络、模型、应用或 UI。

这些证据只验内部完整性、拒绝、幂等和出版回执合同，fakechecker不证明真实无知识／关系语义。精确 capture 分配、生产媒体保留门槛、同题冻结关系、wiki worker 接线与可重建正式投影仍未完成；有嵌套依赖仍保守待确认，不能把本模块提交称为主链已上线或正式发行。

## 2026-10-08 A1：过程存储与保守保留候选（未运行测试）

主控只授权 database/wiki_schema/Store/media_lifecycle/TemporaryArtifacts 及对应测试、本文 QA 追加。自身 session JSONL 最新启动记录为 gpt-6.1-sol/medium，未用父 session ID。没有修改 app/worker/pipeline/collections/Captures/raw/wiki 两候选/kit/模板/静态/路由/飞书，没有接真实数据或运行迁移。

- schema24 在原22→23之后追加：items/collection_operations 的 ingestion_contract/source_binding_sha256/relation_binding_sha256，wiki_tasks 的 outcome_contract/plan_json/plan_sha256，append-only ingestion_events/wiki_outcome_receipts 与最少唯一/FK索引。23 显式保留为允许版本；新库/升级共用同一事务，末尾 FK 检查，旧内容、状态、ID、媒体历史水位不重写。
- 创建与提交默认 legacy；claim/requeue/retry/state/phase 没有新合同排除分支。新增显式参数仅供后续新合同调用和本次合成夹具，不是产品 feature flag。contract/来源/关系绑定不可改写，已关联新 owner 不可脱离或删除；集合成员与 operation 合同匹配，防止借 legacy owner 绕过保留。
- `Store.append_ingestion_event(item_id, kind, code)` 只接受 source_ready/source_fact_ready 和 raw_pending 的 context_pending/readback_pending/writer_pending；自身从 DB 读 SourceFact 生成 ID/hash manifest，无 caller manifest/正文/异常字符串/布尔成功参数。BEGIN IMMEDIATE + deterministic event_key 幂等，detail exact typed keys/固定码由 SQL trigger 再检查；事件无 update/delete。
- A1 不提供 filesystem proof producer。API 拒绝 raw_verified/release_authorized/media_released，SQL trigger 也拒绝这些 kind；wiki accepted 同样拒绝。validated receipt 只是预留候选存储，不能从其 JSON 推出支持通过或出版成功。
- 新合同所有 submitted bytes、source_media 和恢复工作目录保守留存：SourceFact、written_at、旧 kr、succeeded、dismissal、TTL、过程事件都不能释放。legacy-only 材料沿原策略，旧 source_media immutable trigger 原文不改、历史水位不扩大；新保护使用额外 veto trigger，并禁止 submitted owner 搬到 legacy。

未来 A2 的具体 proof 边界：持实际 VaultWriteLock，从权威当前 SourceFact/latest capture decision/current raw/完整关系取快照，再使用原 writer 与只读 bytes/hash/ID/身份/格式/附件核验；事务内重读这些绑定后提交不可伪造的过程证明。RawReceipt 不是可由 caller 任意构造即信任的证书，也不能写 published=True。A1 没有实现此接口；启用生产证明/释放前，主控必须重新串行锁 database/media_lifecycle/Store，并审阅替换当前 fail-closed trigger 的真实实现和候选 schema 升级路径，不能直接删 trigger 放行。现 schema24 候选库 reinitialize 不会暗中更换门槛，旧记录不回填。

新增 test_ingestion_storage.py 设计覆盖：全新默认 legacy；真实合成22/23升级全原列/字节与水位保留、22原FK路径、末尾FK失败、24完整DDL之后失败回滚/重新初始化；绑定/owner/集合不可绕过；新来源在Fact/TTL/reject、共享owner各状态、written raw情况下保媒体/目录；typed事件拒绝正文、proof kind与直接SQL伪成功；事务中断和两进程幂等；预留receipt append-only/FK与accept拒绝。现 test_wiki_schema.py 的版本期待更新到24，旧schema21/22全部原字段仍比较，仅精确新增列/对象投影掉；未来版本拒绝改为25，旧失败证据不删。

本 worker 仅 AST 解析七个 Python 文件和 git diff --check，未 import 业务/执行SQL/pytest/模型/网络/真实DB/Vault/部署。运行时SQL、触发器、迁移和文件保留效果尚未实测；Luna须经主控读实际diff后在CPython3.11.16/freshroot/env -i执行限定七测试文件：test_ingestion_storage、test_wiki_schema、test_store、test_media_lifecycle、test_temporary_artifacts、test_confirmation_schema，以及原test_ingestion（新字段兼容）。不能把原78 distinct候选通过作为A1通过。

精确变更、SHA、未验项和待执行命令归入 `/tmp/kd-v3-r01-a1-handoff-20261008.md`。A1完成后冻结，不继续A2/A3/B，不提交或安装正式产品。

### A1 初始绑定与未发行 schema24 策略澄清

`create_item/submit_source` 的 `source_binding_sha256` 绑定的是**创建时可获得的不可变投递命名空间/输入快照**（例如入口、消息/提交身份、输入版本与确切输入摘要），不是尚不存在的 material/最终 SourceFact。`relation_binding_sha256` 绑定的是**同次投递的冻结关系计划**（有序对象选择器/消息引用、范围及明确的待解析状态），不是尚未分配的最终 raw ID 或完成证明。未知关系可以在计划中明确 pending；不得把它假称已完成，也不得后来改写初始 hash 迁就解析结果。

实际 A1 `_ingestion_binding` 只检查合同与两个 hash 的格式，创建时不读取或要求最终 Fact；`source_ready` 才单独要求已有 Fact，`raw_pending` 在无 Fact 时允许 manifest 两项为 null。因此当前代码没有“先有最终 Fact 才能创建”的要求。A1 也尚未计算/验证投递 manifest 的规范序列化或真实性，合成夹具的 a/b hash 只是占位；产品不能据此声称已有输入绑定证明。A2 的最终 SourceFact ID/hash、capture 最新完整 decision/revision、当前原件/附件与解析后完整关系 manifest，必须由独立 verified 证据绑定到这个初始投递对象；初始两 hash 与 source_ready 观察不能代替它。初始计划不能支持合法解析时保持 pending/交主控裁决，不自行改合同。

本次 schema24 **只是未发行候选**。主控可在后续 A2 串行授权并审阅后扩充同一候选迁移，不要求为了候选阶段的继续开发立即升25。扩充后的全新合成根或22/23→候选24路径应完整验收；已经初始化的旧候选24合成库，须明确重建可丢弃夹具或另审显式候选升级办法，不能靠 `reinitialize(24)` 静默移除 proof/保留 guard。正式发行后再按已发布版本兼容合同制定升级，不把候选策略用于真实库。A2继续暂停，等待主控实际diff/tests审阅与后续授权；本澄清仅修改文档。

### A1 首测失败与水位夹具修复（未复验）

主控已读取 Luna 首测实际栈：123 collected、121 passed、2 failed。两个失败为 `test_upgrade_preserves_every_original_column_and_immutable_byte[22]` / `[23]`，在原新测试第83行读取 `media_lifecycle` 水位时没有 singleton row。`_create_schema22` 是空 schema fixture；原 `_prior` 只 UPDATE 空表，不会造出 singleton。生产 `media_lifecycle.migrate` 则明确 INSERT singleton，因此这是合成前置状态遗漏，不是授权删除生产保留门槛或修改业务迁移的理由。原首测失败、栈和运行证据保留；本 worker 没有读取或改写其 JUnit/原栈文件，不虚构运行路径或摘要。

本次仅将 `_prior` 的 UPDATE 改为显式 INSERT `(singleton,legacy_material_id,released_bytes,compacted_bytes)=(1,40,128,64)`；两个非零计数表示合成的已有生命周期状态，既有“所有原列逐值保留”断言一起检验它们。原水位40断言、全部immutable/业务trigger、旧schema fixture、五业务文件和test_wiki_schema均不改。只做AST/实际diff与SHA读回，不运行pytest/SQL/模型/网络/真实数据或A2；修复尚未验证通过。

修复交接为 `/tmp/kd-v3-r01-a1-fixture-fix-20261008.md`。主控审此具体diff后，仅授权Luna在freshroot定向运行上述测试的22/23两个参数；不重跑七文件，不将定向复验包装为一次新的123全测，也不删除原121/2失败记录。

### A1 主控定向复验验收

首测根 `/tmp/kd-v3-r01-a1-tests-20261008.k6xU15`、JUnit SHA `9829dc2946898410c068df42b6a589a946f5b024d90b32bc4813ad5bf34b328e`，123项中121通过2夹具失败。只修水位夹具后，Luna/max 在新0700根 `/tmp/kd-v3-r01-a1-fixture-tests-20261008.9M3hUa`、env-i、CPython3.11.16测试工具及SQLite3.53.1下唯一一次定向复验22/23两个节点，2 passed、无失败/错误/跳过，pytest/外层均退出0。JUnit SHA `ccd9bf0438c2276b30abf96a7363e000b2b21a0011ac473577a874176872f417`；主控独立解析两份实际JUnit并核五业务/既有wiki_schema测试前后SHA不变，修后新测试SHA `3e113797e32e7135d373c64368b4164a16519ed95595e7d6eb90bd2bf265ebd9`。

全部123个不同节点已覆盖通过，不冒称修后重跑七文件全套。主控已审实际五业务diff、全部新测试和既有测试diff：schema22/23迁移事务、旧列/字节/ID/水位、默认legacy、不可变绑定、保守保留、typed观察及并发幂等在限定合成范围内成立。A1仍无filesystem proof producer、产品接线、真实资料迁移或Vault写入；本验收不把typed事件或预留receipt当原件/出版成功，也不证明正式应用升级。

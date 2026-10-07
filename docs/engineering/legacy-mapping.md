# 旧资产五项映射内部编译器

2026-10-08：本模块只做开发与合成计划，不执行真实迁移。规则依据父工程
`docs/references/prior-v3/legacy-mapping.md` 第 6 节；父会话
`01a11560-64c7-77b2-b011-061056ccabe6` 在 `2026-10-07T20:53:03.829Z`
收到明确答复“同意这五项规则（推荐）”。当次问答明确没有授权真实资料迁移。

主控内部验收补记：已实际审阅编译器及合成测试。独立 Luna/max 回归为 69 passed、无失败／错误／跳过，pytest 退出 0；证据 `/tmp/kd-v3-legacy-tests-20261008.md`，可丢弃目录 `/tmp/kd-v3-legacy-tests-20261008.S5Cvca`，JUnit SHA256 `36d057a78b55b375937c14e9819a59ac2e71f54efc56c9b6cb5c3a8394b56946`。主控另读实际 JUnit、核对源码与测试 SHA。结论只覆盖合成计划；旧数据库适配、真实编号分配、写入或真实迁移没有执行，也不属于本次批准。

入口：`knowledge_distiller.v1.legacy_mapping.compile_plan(facts, *,
previous_receipts=(), occupied_targets=()) -> MappingPlan`。
这是纯函数：无默认 root，无数据库、Vault、编号分配、文件读取、写入、CLI、
模型调用、Store、router、UI 或发布接入；不改旧 `raw_migration.py`。

## 输入及身份前提

全部输入由调用方显式提供；当前测试输入全为合成事实。没有真实旧数据库适配器。

| 输入 | 合同 |
|---|---|
| `LegacyKey` | 明示 `collection / record / version`，三者必须齐全，不从标题推断 |
| `SourceEvidence` | 精确 key、引用路径、原始 bytes、SHA-256、role、已有 raw_id 和 raw_identity；原文来源 bytes 必须与 `LegacyFact.text` 的 UTF-8 字节完全一致 |
| `LegacyFact` | kind、旧编号、精确 text、原始导出证据、lineage、expected_lineage、历史动作、父目标、确认、原 action 时间；AI 必须有完整 `legacy_context` 旧版本谱系清单字节，可携带 `lineage_issues` |
| `expected_lineage` | 明示完整预期来源边界；与实际来源 key 集合不完全一致时阻塞该条，不允许丢掉缺失来源后继续 |
| `HistoricalAction` | 明示动作 key、精确 parent、decision、带 offset 的时间、证据；改观必须引用同 parent 的精确 rethink 动作，且时间不早于该动作 |
| `IdentityConfirmation` | 显式用户裁决：input_key、parent、text_sha256、author、expression、裁决证据。只有 `self + independent` 且完整绑定才产生原话 raw 候选 |
| `previous_receipts` | 调用方提供的私有不可变输入版本账本；同 key/version 内容有变化就拒绝。持久化与读回由未来私有适配器负责 |
| `occupied_targets` | 调用方显式冻结的路径、hash、owner_external_id；没有所有权的同名人工页即使字节相同也阻塞 |

`SourceEvidence.role` 的值为 `raw / source_page / legacy_record`。
AI 的最终依据只接受明确身份及已有编号的 `raw/…/R-*.md#^source-N`；必须校验
编号与路径、raw 信封身份一致，锚点确实存在于正文。不能以来源页替代未解析的最终
raw，也不能指向综合页或把同批旧 AI、用户原话、动作或 Topic 当支持依据。输入／动作／身份裁决的
导出证据以及精确父目标上下文可使用 `private/legacy/…` 下的 `.txt/.json/.md`。
这些上下文仅在私有收据保留，不升级为知识证据。

AI 的 `legacy_context` 必须是显式 JSON 清单：`complete: true`、精确 `version_key`、
完整 `terminal_sources` key 列表、明确 `dependencies` 列表与 `previous_version`。
dependency 项为 `{"key": {collection, record, version}, "sha256": ...}`，必须在显式
证据中找到匹配 key/hash；previous_version 为 null 或同结构，并且必须属于同一
collection/record 的不同版本。所有清单字节原样私留，不生成关系。terminal_sources
必须精确匹配 expected_lineage。缺清单、缺 dependency、版本冲突或 complete 非 true
都阻塞该 AI 条目。清单可包含额外完整旧图谱导出字段，私有收据不会删除这些字段。
若私有账本已经存在该 record 的其他版本，新的 AI 版本不能把 previous_version
声明为 null 来跳过前版；已经在账本中的历史版本仍可原样重复编译。

真实适配器日后必须先验证原数据库的 participant、实际使用的关系版本、前版链、
替代／失格、接受生命周期以及冻结事件边界，完整导出上下文到 `legacy_context`，
把缺失／冲突逐条带入 `lineage_issues`，并提供全部预期终点。编译器不能从合成
事实对象反向证明真实数据库已经完整导出；缺真实适配器不代表真实迁移已经可用。
既有编号只原样保留于元数据，不改正式编号，也不触碰计数器。

## 五项输出

1. `interesting` 或完整 `rethink → interesting_after_rethink` 历史产生
   `ai_synthesis` 候选，路径 `wiki/综合/旧版AI派生-<digest>.md`。明确是旧版 AI
   派生，接受仅历史保留动作，不是事实核验、用户事实或认知；综合不能成为依据。
   未接受 AI 保留只读历史。
2. 动作事实为 `historical_receipt`，不构造原话、不产生 raw；原动作及两步改观史
   在私有收据逐项保留，不改原 rethink。
3. 用户原文字节先保留在私有收据。未知作者、第三方引用、附言或操作理由均为
   `private_retained`。只有精确身份与父目标确认后的独立自述才一输入一
   `user_raw` 候选；路径 `private/raw-candidates/<digest>.md`，不在正式 raw 根，
   不分配 `R-*` 编号，不执行 raw 写入。用户文字与 AI 文字分属不同事实／候选。
   这与**新的真实本人附言**不同：新附言仍由正常采集／身份裁决／raw 链按
   `本人附言` 与明确父目标处理。本 legacy 编译器仅解释历史输入，不代替新附言
   的身份裁决，也不因旧字段名、批注长度或操作动作推导用户认知。
4. 完整 Topic 导出通过 `topic_complete=True` 明示完整性，原始 name/scope/member/
   order/snapshot 全文保存为 `readonly_history` 私有索引项；不产生新 Topic 页面
   或 SQL 写操作。V3 主题未来仍由 raw/wiki 重新生成。
5. 缺身份、缺／冲突谱系、SHA 不匹配、缺锚点、改观异常、同版本改稿、人工页或
   已改写目标冲突，仅阻塞相关 receipt；保留全部旧事实，不产生半份多来源候选。
   共用某个矛盾来源版本的依赖条目共同阻塞，无关条目继续。不猜相似标题关系。

AI 综合候选的 frontmatter 还保留每个最终 raw 的精确旧来源 key/version、已有
编号、身份、锚点引用和来源 SHA，以及原始旧版谱系上下文 SHA；完整上下文在私有
收据中原样保留。最终 raw 的作者身份、编号或锚点缺一都不生成候选。

每条收据包括明确 status、固定 reason、稳定 receipt ID、input SHA 和完整私有
输入（内含每份来源 SHA 与引用路径）；每个候选有 content bytes、候选 hash、
外部 ID、旧编号和精确引用。输出均是提案，不是已迁移或已发布状态。

## 稳定性、材料注入和保密

ID 是 `kd-legacy:<domain>:SHA256([contract, domain, collection/record/version])`。
receipt、AI 综合和用户 raw 候选的 domain 不同；不借用正式 raw、wiki 或数据库
身份。版本变化产生不同候选路径，旧路径保留；同版本内容变化在账本比对时阻塞。
输入按稳定 ID/hash 排序，相同事实去重，无墙钟／随机数。输入顺序不同、相同输入
重复及带原收据重启重新编译均产生相同 private plan bytes。

正文只作为素材。AI Markdown／HTML／伪 frontmatter 放在动态长度围栏里，不能
逃出该围栏；自由 frontmatter 值一律使用 JSON 双引号标量，换行不会增加字段。
用户原话不修剪空白、不合并、不改写；私有输入保存完整 Unicode 和导出字节。
候选路径只由完整 digest 生成，自由标题不进入路径。引用拒绝绝对路径、穿越、
反斜杠、百分号编码、控制字符、wikilink 注入和非法锚点，并验证锚点存在于来源字节。

`MappingPlan.private_bytes()`、`PrivateReceipt.private_input` 及候选正文都包含
敏感内容，必须留在产品私有计划，不能送往普通日志、公开文件、UI telemetry 或
`wiki/log.md`。本模块不会写文件；未来保存方应使用私有目录 0700／文件 0600。
`public_summary()` 只含合同、dry_run、数量和固定状态聚合，无正文、标题、身份、
引用路径或来源 hash。实例默认 repr 也不应代替该聚合接口用于日志。

现存目标只有 external ID 与 hash 都与候选一致时视为相同提案；仍无 overwrite
动作。空占用清单只足够离线规划，不能证明正式目标空闲。真实执行方仍须取得另行
迁移授权、冻结库存、拒绝符号链接、持锁并在写前复核；本编译器不提供发布权限。

## 合成验收与当前边界

`tests/v1/test_legacy_mapping.py` 设计覆盖五规则、直接接受／改观、未知作者／引用、
精确原文与多输入隔离、身份确认绑定、Topic 完整索引、SHA 与引用注入、多锚点、
多来源局部失败、共享来源冲突、重启／重复稳定、同版本改稿／新版本路径分离、人工页
与改写冲突、domain 分离、最终 raw 信封身份／编号／正文锚点、旧版本清单／依赖证据
与无敏感字段的公开聚合。

开发子 Agent 仅做静态语法检查，**没有运行 pytest**。由主控安排已核实的
`gpt-5.6-luna / max` 使用契约 CPython 3.11.16 执行；明确测试目录例如
`/tmp/kd-v3-legacy-mapping-tests-20261008`：

```sh
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src /absolute/CPython-3.11.16/bin/python -m pytest -q -p no:cacheprovider --basetemp=/tmp/kd-v3-legacy-mapping-tests-20261008 tests/v1/test_legacy_mapping.py
```

命令从 source 根执行，解释器路径须由主控核实。所有正文、身份、编号与日期为合成。
未读取真实 DB/Vault，不启动正式应用／模型服务，无真实写入、安装、commit、push、
merge 或 release。候选生成不等于迁移或发布，测试通过也不等于真实数据验收。

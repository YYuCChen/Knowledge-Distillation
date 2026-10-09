# Wiki 独立任务与 Vault 写协议

当前实现核对：2026-10-09，Asia/Taipei，源码业务基线 `e4283034f05778020d159742d1db0a1d904f97cc`。本文定义 V3 wiki 维护任务的持久边界、隔离 staging、typed runner、批次状态、共享写锁和 Vault 工具包所有权。`WikiWorkflow.submit_all`、`WikiWorker._run_typed_locked`、来源／四维／R14 支持检查、journal 发布及 `accept_published_batch` 已有实际应用调用链；旧 SQL derive 不是此链。阶段 1–4 的合成证据是历史记录，不能替代当前验收。按主控当前证据，真实 fresh 整链尚未 passed，actual accepted 仍为 0，发布未完成；本次文档维护未读取正式数据、未运行重复测试。

## 1. 任务边界

当前数据库为 Schema 27；以下 Schema 22／23 描述为表的历史引入阶段。Schema 22 在 V1 数据库中增加三张任务表：

- `wiki_tasks` 保存 Vault 稳定键、请求类型、触发入口、backend/model/effort、工具包版本与清单摘要、冻结边界摘要、状态、计数、固定错误码、恢复状态和程序时间；
- `wiki_task_batches` 保存批次号、条目数、状态和固定错误码；
- `wiki_task_raw` 保存任务内 raw 的稳定编号、身份、相对路径、字节数、SHA-256、顺序和批次号。

Schema 23 增加 `wiki_observations`。它为每个 Vault 保存最近一次后台扫描的 pending/candidate 计数、程序时间、关联任务和固定错误码；未知计数写 `NULL`。观测不保存正文，也不是任务冻结或发布的权威边界。首次启动、任务结束和显式刷新会唤醒后台有界扫描；首页 GET 只读这张表和任务表，不执行网络探针或全 Vault 扫描。

上述任务／观测表不保存 raw 正文、页面正文、标题、用户名、token、任意异常字符串或模型输出。`wiki_task_raw` 一经写入不可修改、删除；任务边界字段和批次身份同样不可修改。typed 任务另有 `outcome_contract/plan_json/plan_sha256`，程序计划固定 raw／批次、来源要求、kit、执行政策和三个 schema 摘要；legacy 保留旧六键唯一性，typed 去重另包含合同及计划摘要。相同执行身份的重复提交返回原任务。每个 `vault_key` 最多有一个活跃任务。`failed` 保留为历史；只有没有任何未完成恢复现场且 backend/model/effort 或工具包清单发生明确变化时，用户提交才可为剩余 pending 建立新任务。原失败任务、冻结 raw 和已发布批次不修改。不同 Vault 不互相阻塞。

Schema 27 的正式 `wiki_outcome_receipts` 是独立收据边界，不属于上面的无正文任务／观测投影。accepted 九字段为 `receipt_id/task_id/batch_no/phase/contract/boundary_sha256/plan_sha256/payload_json/created_at`。`WikiTaskStore.accept_published_batch` 在 `BEGIN IMMEDIATE` 中校验计划并调用 verify，只有精确九字段的一次性连接权限可插入 accepted；普通连接默认拒绝。同一事务推进批次 succeeded 和 completed_batch_count；已 succeeded 不重复计数，已有 accepted 必须 receipt_id／payload 精确相同。

Schema 21 到 22 的任务表、22 到 23 的受限任务表重建与观测表均在各自事务内升级，不重用旧 `organization` 表，也不改旧业务行。旧程序看到未来 schema 时应拒绝打开；回退依靠升级前备份恢复，不提供删除新表的有损降级。

## 2. 状态与恢复

任务和批次都使用：

```text
queued → preparing → running → validating → publishing → succeeded
                              ↘ 任一非终态可进入 failed
failed → queued（仅显式 retry，且恢复不需要或已经成功）
```

`succeeded` 表示发布完成后已经从目标路径读回验证。只通过检查、只生成暂存文件或只发出写命令都不算成功。

多批任务进入 `running` 后，任务保持该状态，各批依次走完自己的 `queued` 到 `succeeded`。所有批次都发布并读回后，任务才统一进入 `validating`、`publishing` 和 `succeeded`。这样下一批不需要让任务状态倒退。`completed_batch_count` 只在批次发布读回成功时增加，任务成功前必须等于 `batch_count`。

失败只存 [固定错误码](../../src/knowledge_distiller/v1/wiki_schema.py)，不存原始异常文本。typed 发布前失败保留 checkpoint，以 `recovery_phase=none` 进入显式 retry；保留 checkpoint 不等于存在待恢复发布 journal。已有发布 journal 的异常使用 publishing 恢复阶段，恢复按 `required → running → succeeded/failed` 推进。只有 `not_needed` 或 `succeeded` 才能显式 retry。发布异常留下的 `failed` 任务由显式恢复入口处理；该入口只核对或回滚发布现场，不自动重跑模型。恢复成功后仍须显式 retry，已经由 journal 和 accepted 证明完成的 typed 批次不会重复运行。

相同执行身份继续显式 retry 原任务；设置改变后的新任务也必须重新权威冻结剩余 pending。任一历史失败任务仍有 `required`、`running` 或 `failed` 恢复状态时，匹配旧任务和新建任务都拒绝，避免未恢复 journal 被另一条入口静默越过。

## 3. 只读扫描与冻结

`tools/kb.py protocol-scan` 复用现有 Vault 规则，依次加载页面和 raw、建立图、计算状态、生成内存派生结果并执行 `Vault.check()`。`pending` 因而仍是当前规则定义的集合：未被取代、没有被 wiki 引用、也没有记入 `wiki/log.md` 的 raw。协议输出只含：

- 错误、提醒、信息的聚合计数；
- 待处理 raw 的相对路径、编号、身份、收录时间、附言对象、邻接编号；
- 文件字节数与 SHA-256；
- 待确认候选数；
- 体检是否具备前提、是否到期、固定到期原因、最近有效体检日期和有效体检次数。

它不输出标题或正文。任何 `错误` 都阻止建任务；不能读取、非法编码、符号链接等 raw 不能静默漏项。日期无效或位于未来时，健康状态为 `unknown`，不能据此宣称无需体检。`protocol-scan` 是只读命令，不能把 `kb.py --dry-run` 当前的进程退出码当作“零错误”，必须读取结构化计数。控制器以 `-E -B`、最小环境和经清单验证的仓库工具执行协议，不执行 Agent 可修改的 staging 工具副本。

应用在持有 Vault 写锁时再次以 `O_NOFOLLOW` 打开每个待处理文件，逐级拒绝路径穿越和符号链接，并核对协议给出的字节数和摘要。任务创建事务只写这次冻结的清单。此后到达的新 raw 属于下一次扫描，不追加进旧任务。

## 4. 确定性分批

raw 先按包含收录日期和稳定编号的相对路径排序。`附言对象` 和 `邻接` 只在本次待处理集合内形成不可拆分的候选关联组；邻接只代表时间相近，不能宣称真实因果。

含 `本人` 或 `本人附言` 的关联组优先，然后处理其余组。按顺序把完整关联组装入约 5 份的批次；一个关联组超过 5 份时整组保留。`all` 覆盖本次冻结集合的全部批次；手动 `one_batch` 只冻结计划中的第一批。Local Web 只提交 `all`，不对用户增加新的批次选项。

## 5. 共享 Vault 写锁

应用和手动维护共同锁定 Vault 根目录的 inode，使用操作系统进程锁自动释放。实现不创建 PID 锁文件，不按进程号或时长猜测并删除“旧锁”。打开根目录前逐级 `lstat`，拒绝调用路径中的符号链接，并在加锁后核对设备号和 inode。

手动 Agent 必须由 `tools/wiki_session.py` 包装整个编辑命令。包装器的 broker 持有目录锁；直接 Agent 继承锁 FD。Agent 用默认 `close_fds=True` 启动的孙进程无法继承该 FD，所以 broker 还在 0700 临时目录创建本地 Unix socket，以随机 nonce 和 Vault 稳定键回应活跃会话验证。`kb.py` 的写命令要求真实 FD 或成功的 socket 握手，环境变量本身不构成证明。包装器 launcher 被异常杀死时，broker 与 Agent 仍保持编辑窗口和锁；Agent 结束后 broker 退出，内核释放锁。

阶段 1 已用合成 Vault 验证包装器、模拟 Agent、默认关闭额外 FD 的孙进程和跨进程互斥。阶段 2 runner 使用同一 broker 为受限 Codex CLI 子进程提供精确到本次 socket 的会话证明，并在超时或取消时终止整个进程组。现有 Claudian GUI 插件仍未接入这一启动方式；它不在包装器内时会安全拒绝写入。真实 GUI 入口尚未端到端验证，不能因协议测试通过就宣称可用。

## 6. Staging、批次校验与发布恢复

每个任务使用产品私有目录 `wiki-tasks/<task_id>/`。每次执行在 `attempts/<随机标识>/` 下新建 `workspace/`、`control/` 和 `backup/`，完整快照落盘后再原子更新 0600 的 `current-attempt` 指针。`workspace/` 是 Agent 唯一可写工作区，`control/` 保存控制器快照和发布 journal，`backup/` 预留给私有恢复材料。三者均不位于正式 Vault，Agent 不获得正式 Vault 路径或写权限。工作目录 `cwd` 只是定位；实际隔离还必须由 Codex OS sandbox 限制写根、代理网络、外部工具和 Unix socket。网络代理采用空域名白名单，只为本次 broker socket 建立精确允许项。

legacy 执行保留原新 attempt 规则。typed 执行首次建立 `execution/binding.json`，后续 `load_bound_staging` 核验 current-attempt、计划、kit、来源及布局后恢复同一绑定 staging，不另造 attempt 重置预算；模型子进程通过录制、broker 和有界 pipe pump 管理，不能把重新打开 workspace 当作旧进程已结束的证明。typed 当前不会在成功后自动清理绑定 checkpoint。未知旧 attempt 不按 PID 或时间猜测删除。

每批 `execution/batch-N` 保留完整 baseline 和 before 字节、生成 reservation/result/proposal、检查／体检阶段收据、final-stage 与 validated receipt ID。生成在 spawn 前持久占额；正常结果复用已核验 checkpoint，未知结果允许消耗额度后有界再生，最多三次生成 reservation，不把未知完成当成功。支持修复使用原 Gate 的两次额外预算，跨重启保留。`validated.json` 已存在时严格比对同一 receipt_id，相同复用、不同拒绝，不覆盖文件；进入 publishing 后按 journal 恢复，不再次生成或发布。

新 generation reservation 和 check／health 等阶段记录在 before_spawn 持久化 Asia/Taipei `started_at`，旧无时间记录不补造。generation／health 提示的 `host_context` 提供活动日期和当次扫描计数，spawn 跨日不匹配即拒绝。最终 program scan 将首次 `issue_counts/candidate_count/observed_at` 与完整候选树、plan SHA、已验证阶段记录绑定；同候选跨日读回既有观测，不按今日重算（`e428303`）。这些管理事实不能替代 raw 知识判断或证明完整阅读、独立 skip 及历史动作。

已保存 final-stage、已发布及已成功批次的恢复使用 `_typed_validate(regenerate=False)`：只读核验受保护文件、kit 和候选，不重跑会改日期的 kb 派生写入。已有 Gate 候选证书经 cached check／树及 Registry 摘要严格匹配后复用，不能借缓存放行新候选；支持响应仍严格重验（`a096bdc`）。

任务准备时复制任务开始可见的全部 raw，便于验证旧 wiki 引用；只有数据库冻结的 raw 能进入本任务批次。任务创建后到达的 raw 即使在 worker 准备前被复制，也必须保持 pending，不能被本批提前消费。每批校验要求 pending 集合恰好减少本批路径，raw 字节、工具包、未知文件和 `.graph` 中非白名单文件保持不变。允许发布的 `.graph` 文件仅为 `graph.json`、`state.json` 和 `检查结果.md`；例如可能含用户查询的 `queries.jsonl` 不是可再生白名单。

最后一批 ingest 后，控制器重新读取结构化 `candidate_count` 和 health 字段。自动体检仅在素材与候选均清零，并且从未有效体检，或上次有效体检后发生 ingest/confirm 且已满七天时执行。体检使用独立 runner 调用；必须新增一条有效 lint 记录并改变体检报告，失败则整批不发布。晚到 raw 会使本轮不宣称清零，也不执行自动体检。

发布在持有正式 Vault 锁时逐文件比较 before 摘要、原子替换并 fsync；成功只在正式路径按 after 摘要读回后记录。恢复以私有 journal 的 after 摘要为权威，不以可能被 Agent 后改的 staging 文件为唯一证据；同时复核未替换 wiki、全部原 raw、工具包和冻结 pending 边界。用户在崩溃后修改过目标时保留用户内容并进入固定冲突状态。

typed 各阶段传完整冻结上下文 C，只消费当前批次 B；proposal 的 processed_with_knowledge／processed_no_knowledge 均要求非空 documents 和最终文件 SHA，正常无知识可只列 wiki/log.md，unknown 保留 pending。独立 check 核 definition／method／reference_lead／relations 四维及逐字证据范围，不按长度／空 points 宣告无知识。体检后的文件摘要由宿主刷新，必要时再核对。随后 `_typed_candidate` 构建最终 Registry、可信 GeneratedSection 与 program_facts，调用 R14 Gate；详见 [wiki-support.md](wiki-support.md)。Gate supported_candidate_not_published 只是候选支持，不能作正式 accepted。

`_accept_typed` 在正式库接受事务中重新核验候选收据、完整 C、最终 documents、check／support、已提交 journal 和正式目标读回，并用同连接的 `trusted_source_callback` 重核来源摘要。通过后才由 `accept_published_batch` 写正式 accepted 并完成批次；支持检查成功、CLI 退出 0 或 staging 有文件均不足以替代这一过程。

## 7. 工具包所有权

仓库中的 `vault-kit/kit-manifest.json` 记录工具包版本、协议版本，以及本次工具包拥有文件从源码路径到安装路径的逐文件 SHA-256。拥有范围是 Vault 根 `AGENTS.md`、`CLAUDE.md`、`tools/kb.py`、`tools/wiki_session.py`、`tools/wiki_display.py`、`.kd/assets/kd-wiki.css` 和四个 `.agents/skills/*/SKILL.md`。

安装后收据位于 `.kd/wiki-kit.json`。`.kd/` 是产品自有的隐藏协议目录，只保存版本、路径和摘要，不保存用户正文。`wiki/`、`raw/` 和用户 `skills/` 不属于工具包。升级计划会先验证当前收据和所有已安装摘要：用户修改过的拥有文件、符号链接、没有收据的同名目标，以及新清单想接管但旧收据未拥有的目标都保守拒绝覆盖。

V2.0 正式标签 `v2026.09.30.4` 尚未写入收据，但其 `vault-kit/README.md` 明确声明了手工安装的七个文件。V3 只把该标签提交中七个安装路径的逐字节 SHA-256 作为一个封闭的历史所有权身份：七项必须全部存在并精确匹配，三个 V3 新增目标必须全部不存在，才在用户显式点击现有“修复工具”后进入同一安装事务。任一缺失、改动、链接、非普通文件或新增目标冲突都按未知文件处理，保留现状且不提供覆盖动作；不按版本字样或相似内容猜测身份。事务直接写入 V3 文件和 V3 收据，不先制造中间 V2 收据，也不读取或修改 `wiki/`、`raw/`、`.graph` 与用户 `skills/`。

阶段 4 的 `WikiKitInstaller` 在同一个 admission gate 内先保留应用 worker，再取得 Vault inode 写锁。它把所有目标的 before/after 摘要、权限和必要的原字节备份写入 0700 产品私有 runtime journal，完成 fsync 后才开始原子替换；安装后再次核对 manifest 拥有文件。中断恢复只接受目标仍等于 journal 的 before 或 after 摘要：prepared journal 回滚，committed journal核对成功结果后收口；用户后改产生第三种摘要时保留用户文件并返回固定冲突码。退休清单项不自动删除，恢复日志也不保存正文以外的任何业务内容。设置状态检查只读取 manifest、receipt 与这些小文件，不扫描 `wiki/`、`raw/` 或调用模型。

工具包内的 CSS 资产安装到 `.kd/assets/kd-wiki.css`，不因用户是否启用样式而改变工具包兼容性。`WikiStyleService` 使用独立的 `.kd/wiki-style.json` 收据和独立 journal，把已验证资产安装到 `.obsidian/snippets/kd-wiki.css`；安装不会启用样式。启用和停用只以摘要比较和原子替换修改 `.obsidian/appearance.json` 的 `enabledCssSnippets` 中 `kd-wiki` 这一成员，保留主题、其他 snippet 和未知字段。无收据同名 snippet、符号链接、畸形 appearance，或在规划后摘要复核中发现的后改均保守停止。摘要复核与同一锁约束协作入口，但普通文件系统没有原子 compare-and-swap，不能把它描述成对绕过共享锁的任意写入提供绝对隔离。恢复完成后服务重新报告 `ready`、`missing`、`installed` 或 `enabled` 等当前可观察状态，由设置界面刷新；恢复本身不会自动继续另一项写操作。

阶段 3 的 `WikiKitRuntime` 把可信执行收口为一条运行时契约：源码环境先用绝对 CPython 可执行文件验证运行环境契约，再以 `-E -B`、最小环境执行经过清单验证的源码 kit；冻结环境由主应用的专用 `--wiki-kit` helper 分派 `kb.py` 或 `wiki_session.py`，不依赖外部 Python 或可选 Qwen 组件。自动 runner 提示词使用该 helper 命令；人工技能中的命令仍是人工会话示例。Mac 打包资源由同一清单逐文件验证后加入，不把用户的 wiki/raw 打进应用。

## 8. 应用接入与生命周期

应用由 `WikiWorkflow` 向 Web 暴露只读 `snapshot()`、权威 `submit_all()`、显式 `retry(task_id)` 和异步 `request_refresh()`。状态快照只返回任务身份、计数、固定错误码、恢复状态、受限动作和两个允许的结果相对路径；不返回正文。新 pending 优先显示可提交，活跃任务优先显示自身状态。工具包缺失、漂移或不兼容会持久写入观测，并把设置入口作为恢复动作，避免一次提交响应后的后续 GET 丢失错误。

实际组装为 `app.py` 创建 `WikiWorker(..., source_store=store)`，`WikiWorkflow.submit_all()` 显式传 `outcome_contract='r08-wiki-outcomes-v1'`；worker 按合同分派 typed／legacy，不重分类旧任务。typed runner 使用固定 proposal/check/support schema 与录制 final 输出；应用政策为 `APPLICATION_UTF8_POLICY`，对完整实际 stdin 和 schema 的字节／摘要做有界校验，不以字符估算伪造 token 数，也不声称掌握远端完整请求窗口。真实 CLI 运行与正式 accepted 的验收须分别记录。

主 worker 与 wiki worker 通过同一个可重入 admission gate 协调 Web、飞书入口和更新。更新 reservation 关闭新写入并依次保留两 worker；任一步失败都会按相反顺序释放已保留资源。飞书门禁位于消息、动作、补收和同步的实际入口，未停稳的 runtime 不会被清空或并发重启，较晚的停止或更新保留也会阻止回滚线程重新启动入口。worker 的有界 `stop()` 只有在线程确实退出时才报告成功；runner 在 preflight 与 `Popen` 之间也检查同一取消状态，避免停止请求丢失。macOS 通过 `applicationShouldTerminate_` 延迟终止许可，双 worker 和飞书实际停止后才允许退出；需要重启时也只在该门禁完成后启动重启辅助进程。

## 9. 验证边界

下面阶段记录均为历史验证范围，不是对业务基线 `e4283034f05778020d159742d1db0a1d904f97cc` 全部测试重新通过的声明。当前接线已有 `test_wiki_worker_typed.py` 等合成实际 API 路径，仍不能代替真实 CLI 语义、正式 accepted 或发布验收。本次只改两份文档，不运行模型、矩阵或重复测试。现行主控 gpt-6-astra/medium、全部子 Agent gpt-6.1-sol/medium；历史 Luna 等执行者不作为当前分工。用户已按展示候选批准四项 UI 接入，并同意 R11 Reddit／R12 pyannote 延期；本维护不实施 UI，也不把其他未验事项扩展为已完成。

合成测试覆盖 schema 21 加法升级与事务回滚、未来 schema 拒绝、任务和批次非法转换、失败恢复与显式重试、重复提交、不同 Vault 的活跃任务、60 份以上 raw 的全覆盖分批、晚到 raw、进程重启读回、跨进程及手动会话冲突、launcher 异常退出、孙进程会话验证、工具包漂移和路径符号链接。

阶段 2 另以合成 Vault 覆盖受保护工具被恶意修改而不得由控制器执行、任务冻结后到达的 raw、多 Vault 锁竞争、顽固孙进程超时、发布中断、journal 恢复、恢复后用户改写冲突和显式 retry。所有自动化测试使用显式可丢弃目录与合成正文。阶段 1–2 没有启动正式应用、没有读取或修改正式数据库和 Vault，也没有调用模型处理真实内容。真实 Codex CLI 沙箱探针只使用合成目录；其最终配置证据应单独记录，不能由单元测试替代。

阶段 3 的源码测试验证了绝对 CPython 3.11、恶意环境隔离、清单资源、停止与取消竞态、双 worker reservation、轻量状态和 Schema 23 升级。`--wiki-kit` 冻结分派与资源定义已经进入源码，但 `console=False` 的候选 `.app` 是否能稳定把机器协议 JSON 接回 controller 仍须在阶段 6 用真实候选包验证；模拟 `sys.frozen` 或源码子进程不能代替该证据。

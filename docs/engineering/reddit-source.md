# Reddit 内部采集与精确清除合同

2026-10-08，Asia/Taipei。**独立内部实现，独立合成复验 88 passed；未接产品或 UI。** 用户已批准授权接口／导出路径与受约束 Reddit 来源清除开发特例。没有进行真实采集、授权申请、删除、迁移、Vault 操作或发布。

## 访问与原文

`v1/reddit_source.py` 只转换调用者已取得的 JSON bytes；不使用网络、浏览器、session、cookies、OAuth 客户端或模型。`AccessReceipt` 记录明确授权依据、provider、owner、用途、帖子／评论范围、版本、起止时间、保留期限、policy artifact 与 revision。它不是软件开源许可，也不能自行证明平台已批准用途。正文里声称的许可不是授权输入。

`AccessState` 必须由调用者提供当前 revision、撤销状态、精确停止引用集合与跨版本 `stopped_node_ids`。转换入口、返回前及 `capture.evidence(...)` 均校验授权；过期／撤销／保留期限到期／版本或范围变化拒绝后续处理。调用者传入 aware `now`，原始平台时间保留原值并可解析为 UTC；不更改原始时间或稳定 ID。纯函数不能强制收回其他代码此前持有的内存结果，未来产品消费入口必须重新检查门禁。

两个入口：

- `parse_authorized_api(payload_bytes, *, receipt, state, coverage, now)`：Reddit JSON Listing，支持 t3 原帖、t1 评论、嵌套 replies、more 占位与 Listing 游标。
- `parse_authorized_export(...)`：本合同版本化 JSON，`schema_version=1`，`post` 是 t3 data object，`comments` 是 t1 data objects 数组，`more` 是 more data objects 数组。字段使用原生 `name/parent_id/link_id/author/created_utc/edited/deleted/title/selftext/body/score`。不是任意 CSV、网页或平台导出格式兼容承诺。

`RedditCapture.raw_payload` 保留输入字节与 SHA-256；snapshot 是客观节点标识加完整 title/body 的派生呈现，node_ranges 指向 snapshot 中真实原文子串。标题／正文不 strip、不摘要、不截断，不丢低票反方、作者更正或分支差异。作者删除、正文删除／移除、未知编辑与时间分别保存；不恢复平台未提供的已删正文。结构节点用原生稳定 fullname 与 source version，不以正文／标题猜身份。

迭代恢复 parent tree，深度按实际父链核验；flat export 可以表示深树。Python 标准库 JSON 解码器对极深嵌套仍有递归限制，此时明确 `payload_invalid_json`，不返回截断结果；不是任意深嵌套 API bytes 支持承诺。同节点相同内容去重并诊断；冲突不选胜者，全部原始 bytes 仍留在 capture。未知结构、缺 parent、跨帖、环、缺正文或冲突阻止派生。

## 三个独立状态

| 状态 | 含义 |
|---|---|
| `structure_usable` | 观测结构可恢复；正常 more／上游截断不是结构错误 |
| `derivation_allowed` | 当前授权含 derive，结构可用且未超预算；不要求全帖采集完成 |
| `coverage` | scope、请求／观测排序、node/depth/bytes 预算、观测数量、more、上游截断及诊断 |

明确已授权的部分范围可作为有限证据。coverage 为 `partial`、`unknown` 或 `complete_within_declared_scope`；最后一项仅代表调用者声明范围，不能当作平台全量证明。未知排序、未验证完整性如实记录。预算超限保留完整输入与呈现，但阻止派生，不偷偷丢尾部。`capture.evidence(...)` 返回 coverage 和 `observed_scope_only_not_whole_thread_consensus` 限制；范围有限证据不能宣称全帖共识，点赞不是真实性／共识背书。

### 删除信号与 stop-first 接口

`deleted=true`（即使正文仍完整返回）、`[deleted]`、`[removed]`、显式 removed 或提供方移除标记产生 `DeletionNotice(source_id, observed_version, reason)`。仅 `author='[deleted]'` 不意味着正文删除。capture 的 `erasure_required=true`、`derivation_allowed=false`；`evidence` 明确拒绝 `erasure_required`，不先过滤节点再假装派生成功。原始 bytes/snapshot 仅用于待清除与诊断候选，不是可继续处理的来源事实，不自动 purge。

观察版本不等于删除范围：节点删除／移除影响该稳定节点的受约束历史版本，不能只用当前 `SourceRef` 声称已清历史。注册合成副本时显式给 `deletion_bound_refs`（默认 None 为约束未知），且必须为本项 refs 子集；派生项的约束 refs 由父链并集核验。caller 将 `capture.deletion_notices` 交给 `root.stop_deletion_notices(operation_id=..., notices=..., inventory_complete=..., covered_kinds=...)`，该方法只持久化 stable-node 停止与引用失效、计算已登记受约束版本，返回 `DeletionStop`；不会计划或删除。之后另行 plan/execute。

caller 必须将 `root.stopped_refs()` 和 `root.stopped_node_ids()` 接回 `AccessState`，让历次版本及后续同节点处理均停止。已证明独立用户原话的 original（owner=user、contains_user_content=true、无父、deletion_bound_refs为空）保留，不当作受约束副本；相应引用仍可失效。含受约束内容的混源用户笔记继续 manual。未知历史库存、约束缺失、父约束并集矛盾均 gap/manual，不能完成；后来传入 optimistic 完整标记不能消除已记录的缺口。没有从 stable ID 推断任意第三方内容都属于受约束副本。

`stop_sources` 仅接受授权撤销／过期／保留到期，维持明确版本范围，不扩大到未来独立授权版本；`node_deleted` 必须走 notice 接口，避免误用窄版本范围。原帖删除会停止该 capture 派生并展开原帖的受约束历史/副本；没有单独删除通知／依赖边的独立评论不凭帖子 ID 自动删除。

## 仅限合成副本的清除

`v1/reddit_erasure.py` 不依赖 `Store`、global raw、runner 或 pipeline，也不改不可变触发器。没有产品 CLI 或路由。

1. `SyntheticRoot.create(new_absolute_path)` 只接受不存在的新目录，创建 0700 root、私有 copies 与 0600 无正文 manifest/lock；既存目录不能直接被 create 收编。恢复用 `SyntheticRoot(path)`，核验专有标记、uid、权限与 root dev/inode；路径上有 symlink 拒绝。使用解析后的真实临时父路径（macOS `/tmp` 本身可能是 symlink）。
2. 调用者将合成文件放入 `copies/<artifact_id>.bin`，再 `register_artifact(...)`。仅允许稳定 ASCII identifier 路径，避免收据泄露来源标题或 URL。记录 source IDs/version、父 artifact IDs、kind、归属、synthetic/managed/user 标记、删除约束 refs、inode/size/mtime/ctime/SHA256，不复制正文到 manifest。
3. `stop_sources(operation_id=..., source_refs=..., reason=...)` 或上方 `stop_deletion_notices` 先 fsync 持久化停止集合与程序计算的依赖失效标识。清除取消、失败、重启均不恢复访问；授权到期不阻挡所需本地合成清除。开始任何 operation 后库存冻结，拒绝新注册。
4. `plan_erasure(..., inventory_complete=..., covered_kinds=...)` 计算父谱系来源闭包，不信 `lineage_complete=true` 自报。非 original 必须有父节点，声明 refs 必须与父 refs 并集一致；missing parent、环、未知归属或祖先错误均 manual/gap。由父链发现受影响来源时，即使本项漏报该来源也能选中并停止引用，不能据漏报执行删除。计划覆盖 original/cache/candidate/model_response/staging/backup/ai_derived 七类；空类别也需声明。扫描 copies 中未登记成员并报告 gap，不递归到其他目录。
5. `execute_erasure(plan, synthetic_only=True, cancel_check=...)` 与 `resume_erasure(operation_id)` 持私有协作锁，核验 plan/库存摘要、停止状态、目录身份、文件 inode/hash/时间/uid/nlink。O_NOFOLLOW、dir_fd 防路径穿越；拒绝 symlink、hardlink、非普通文件。durable prepared → 精确 unlink → 父目录 fsync → 无正文清除收据。没有正文备份或 quarantine。

默认 owner=unknown、synthetic/managed=false、contains_user_content=true、lineage_complete=false，保守保护自述。混源纯受管 AI 副本可整文件清除，但不清除其他来源的独立原件／用户原话。含用户文字／手改或无法拆清谱系的混合文件保留为 manual，失效引用保持；不做正文替换，不假称残留内容已清除。计划后的手改亦保护，持久 manual 不因后续恢复旧字节自动重试删除。

prepared 后崩溃，文件已不存在可记录 `absent_after_prepared`，不伪称亲眼执行；没有 prepared 的失踪是 manual。重放不会把新 inode/hash 或清除后重建文件当旧副本。fault seam `_event` 提供 prepared、before_unlink、unlinked、directory_synced、receipt_synced 五个边界；异常留下 durable checkpoint，恢复继续同一 plan。

终态 `cleared_registered_synthetic_scope` 只代表完整声明且可核验的合成清单，始终 `product_complete=false`。manual、unknown、缺库存均 `partial_manual`；取消 `cancelled`。没有含糊 `done=true`。收据仅 ID/version、哈希、时间、状态和稳定错误码，不含原 URL/title/author/body。有限本地 unlink 也不代表物理磁盘、系统快照、外部备份、远端模型服务日志已消除。

锁以 O_NOFOLLOW/O_NONBLOCK 打开，fstat 和 named lstat 核验 regular/uid/nlink/0600，再 flock；flock 前后、持锁上下文出口、每次 journal 保存与 unlink 前都核对 named lock 和所持 fd 的 dev/inode。root marker 绑定初始化 lock inode，不能采用新建替换锁绕过原锁；模块不删除／重开活锁。控制 journal 在读取 bytes 前后都按持有 fd 核验 mode0600，并要求 named inode 一致。FIFO、普通路径／权限替换、会造成多把锁并发分裂的替换均拒绝。新增 marker 字段只供新建合成根；不自动迁移／收编旧 marker。

0700 与协作锁用于受控合成环境，不能防同 uid 恶意进程在最后核验和 syscall 之间蓄意更改对象。本模块不宣称敌对并发下的安全删除系统。

## 产品支持缺口与验收

现有产品 `RawLedger`、`SourceFact`、AI 判断记录仍受不可变 DB 约束；现有后台库存、wiki／缓存／模型响应／staging／备份谱系不保证完整。**本实现不能清干净现有产品，不能代表在线采集、产品删除响应或获准用途已上线。** 未来门禁和库存接入、真实对象授权及可见差异仍由主控另行判断。

Sol 设计并新增两个合成测试文件，Luna/max 负责执行，主控读实际源码及修复、测试与证据后验收。单次两文件回归 88 passed，无失败、错误或跳过，pytest 退出 0（0.30s）。报告 `/tmp/kd-v3-reddit-tests-20261008.md`，数据根 `/private/tmp/kd-v3-reddit-tests-20261008.fc4buh`，JUnit `results.xml` SHA256 `d914a63afec9277d99ba696b1587438d7b4e108baf2e2ab24ff24158a75a1596`；主控独立解析实际 JUnit 并核对四份源／测试执行前后 hash 未变。环境为既有 CPython 3.11.16 测试工具、`env -i`、明确新 source PYTHONPATH、独立 basetemp、禁用第三方插件及 cacheprovider，不重跑、不安装依赖。

覆盖 1200 层 flat tree、低票反方、作者更正／单独删作者可用、已删节点仍返回正文但禁止 evidence、帖子／评论删除跨受约束历史版本、未知库存与父约束矛盾、未来独立版本授权边界、more／截断／预算、授权时限／撤销、原文字节与 ranges、父树冲突、七类副本、混源和默认自述保护、手改、路径／inode／目录边界、取消、故障重启与无正文收据。锁 FIFO 测试用同一解释器的合成子进程和5秒 timeout 保证回归不会无限挂起；flock／unlink 边界替换、journal fd/named inode/mode前后核验用 fault seams。所有清除仅发生在可丢弃合成根；不提升 product_complete，也不证明产品库存或真实删除支持。

政策依据沿用主控已核验的 [官方 Data API Terms](https://redditinc.com/policies/data-api-terms) 2.8/3.1/3.2/6 与父工程 `docs/reddit-access-decision-20261008.md` 的批准记录。本轮未重新联网核验条款；节点跨历史版本与保护用户原话的具体规则来自已批准删除响应合同及本次主控明确细化，不把它包装成新增平台许可或法律结论。

在 source，使用主控已核验 CPython 3.11.16（不安装新环境），Luna 命令：

```text
PYTHONPATH=src <verified-python> -m pytest -p no:cacheprovider --basetemp=<fresh-private-synthetic-dir> tests/v1/test_reddit_source.py tests/v1/test_reddit_erasure.py
```

以上为开发时留下的命令形状，实际单次运行以独立报告为准。后续若有新变更，basetemp 仍必须为新建可丢弃目录；pytest fixture 仅创建新 private synthetic roots，全部授权、帖子、评论、用户文字为编造夹具。未读取正式 data/Vault/profile/凭据，未调用网络或模型。独立合同已验，产品接线及正式发布尚未完成。

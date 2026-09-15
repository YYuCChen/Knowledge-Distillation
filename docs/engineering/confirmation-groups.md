# 判断组与逐位置确认

V1.3 源码合同；真实模型与双平台发行验收独立记录。

`confirmation_groups.form_groups` 只在首次发布时形成组。每批最多 32 个成员，最多一次可选模型分组提议。自动依据是来源中重复的明确同义定义；模型提议须包含来源连续定义、语义词以及每一成员的实际连续上下文。相同拼写、没有发现冲突或模型解释本身都不足以合组。跨媒体、来源版本、候选集合冲突、否定/引用等保守冲突保持单点。已有组不扩大，新增成员另排尾部；组内始终保留各位置身份和原音。

`Distiller.resolve_group(item_id, action, value='', *, token, request_id, group_id, group_revision, selected_member_uids, actor='local')` 支持 candidate/manual/keep/unable。actor 由受信服务端设置。首次调用严格验证当前 token；同次已认证调用遇无关整份 CAS 冲突最多重读 4 次，每次重新验证整组所有成员的语义 revision。跨请求过期报 `group_token_stale`，组变更报 `group_revision_conflict`，持续竞争报 `group_save_busy`。冲突异常可带 `affected_member_uids`，表示需核对的当前组成员，不宣称逐一定位了实际变化。

所有选中位置基于同一原 snapshot 生成编辑计划，按 codepoint 偏移映射剩余疑点、未决项与原音定位。重叠无法证明安全时拒绝整个请求。候选白名单、组依据和包括未选成员在内的 revision 均重新核验。逐成员 audit 与快照/事实、队列映射、幂等 ledger 在同一 Store 事务提交。request_id 优先查账；原请求成功后重放返回已保存状态，不重复编辑或调用模型。

部分确认保留余组的 enqueue_seq；新组尾排。真正多成员组首次发布、组操作或局部重新识别后设置 `group_confirmation_contract=1`。旧单点入口对已标记来源仍只选择一个位置，走相同原子计划并兼容旧 decision ledger。

unable 保留 deferred、原音和未决标记。新组来源有 deferred、review_required 或未解决 uncertainties 时不成立 SourceFact。`finish_transcript`、自动执行及内部 partial 路径不能绕过组未决门禁。旧无组标记历史来源的显式 partial 语义保留，这是兼容差异。

`restore_group_deferred(item_id, *, token, selected_member_uids=None)` 默认恢复全部 deferred；选定成员移回 concerns，原文、同轮身份和原有队列顺序不变，使用整份 CAS。恢复后可逐成员核对并继续提交。

`rerecognize_group(item_id, *, token, request_id, group_id, group_revision, selected_member_uids, actor='local')` 的产品动作是“仅此处重新识别参考”。默认调用者选择一个成员，明确勾选才传多个。实际读取目标确认音频，在独立临时目录调用本地识别器；全部成功后一次提交 recognition_reference 和影响 decision_revision 的 decision_basis。原文和候选不自动替换，其他成员不变。选中成员成为尾部单卡，旧空组 superseded；部分余组保留顺序。失败不写队列/参考，保留原音。旧 `rerecognize` 仍是整份素材操作，不能用作组卡默认动作。

## 验证范围

合成测试覆盖 1/8/32/33 成员、跨段重复、正反向依据、Unicode 偏移、部分确认、未选成员变更、并发重放、CAS 重试上限、SQL 保存中断、局部识别失败、未决恢复、旧单点相邻回归。实际 CPython 3.11.16 运行；本批未运行真实模型、Windows、原生包或飞书/本地界面。源码测试不能证明这些未验项。

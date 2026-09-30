# 项目文档入口

本套文档区分当前能力、产品约定、未来探索和工程操作。编写核对基线是公开源码提交 `7f9c6a149e54b2e11ba1318a55a4efef185d9873`、公开 V1.1 构建 `2026.09.11.16`。后续任务应用前核对相关代码和发布状态。2026-09-12已合并Windows源码与治理文档；本次项目记忆归档核对起点为 `3d69b42d9b768ee559ec387faf149ef660adb53d`，不因此改写各历史资料的验证日期。

当前正式版本为 [V2.0 raw 素材层、飞书随手记与 vault-kit（macOS 构建 2026.09.30.4）](releases/v2.0/release-20260930.md)。V2.0 只维护和验收 macOS；Windows 保留在 [V1.3](releases/v1.3/release-20260915.md)。上述历史核对起点及 [Python 3.11 重做](releases/v1.2/python311.md)保留追溯，不能代替最新交付范围。

| 问题 | 入口 |
|---|---|
| 当前产品做什么 | [产品范围](product/scope.md) |
| 哪些知识和用户数据边界必须保留 | [知识语义](product/semantics.md) |
| 界面修改如何保持用户意图 | [设计维护](product/design.md) |
| 代码职责在哪里 | [架构](engineering/architecture.md) |
| 工作纪律、怎样证明完成 | [测试与证据](engineering/testing.md) |
| Python共同基线与旧环境退役 | [运行环境契约](engineering/python-runtime.md) |
| 源码、构建和发行如何对应 | [发布](engineering/release.md) |
| 什么保留、什么清理 | [存储](engineering/storage.md) |
| 哪些后续优化已提出 | [版本优化待办](roadmap/next-version.md) |
| 远期构想和开放问题是什么 | [探索登记](roadmap/exploration.md) |
| 原始产品讨论和历史取舍在哪里 | [项目记忆归档](history/2026-09-project-memory/README.md) |
| 知识怎样走向实际应用 | [研究报告与证据](research/2026-09-11-knowledge-to-tools/report.md) |
| 项目资料怎样筛查、公开和留存 | [资料治理](engineering/project-memory.md) |
| 为什么分开管理 | [治理决策](decisions/0001-project-authority.md) |
| **二期现在按什么做（现行依据）** | [决策0004 三方对齐](decisions/0004-phase2-alignment.md) |
| 二期怎么施工、先做什么 | [二期施工计划](roadmap/phase2-plan.md) |
| 二期交付了什么、怎样正式收口 | [二期交付报告](releases/phase2/delivery.md)、[V2.0 正式发行](releases/v2.0/release-20260930.md) |
| 应用与知识库之间的接口 | [raw 接口规格](engineering/raw-interface.md) |
| wiki 侧的完整设计 | [个人知识系统方案](product/phase2-knowledge-system.md)、[vault-kit](../vault-kit/README.md) |
| 飞书随手记的需求 | [飞书随手记需求说明](roadmap/handoff-feishu-capture.md) |
| 哪些判断交给 Jev、没配置或出错时怎样 | [Jev 说明](engineering/jev.md) |
| 二期的历史讨论与被部分取代的决策 | [二期讨论进展](roadmap/phase2-discussion.md)、[决策0002](decisions/0002-phase2-knowledge-backbone.md)、[决策0003](decisions/0003-material-foundation.md) |

所有任务先遵守 [AGENTS.md 最高工作原则](../AGENTS.md)：框架对、可用、面向未来即推进；可逆的放手迭代，不可逆的论证清楚。

尚未实现的构想仍有价值；发布包存在不证明所有源码构建路径完整；文档日期不能独立决定其中的意图是否作废。维护时必须保留这些区别。

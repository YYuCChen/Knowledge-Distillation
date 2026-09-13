# 项目文档入口

本套文档区分当前能力、产品约定、未来探索和工程操作。编写核对基线是公开源码提交 `7f9c6a149e54b2e11ba1318a55a4efef185d9873`、公开 V1.1 构建 `2026.09.11.16`。后续任务应用前核对相关代码和发布状态。2026-09-12已合并Windows源码与治理文档；本次项目记忆归档核对起点为 `3d69b42d9b768ee559ec387faf149ef660adb53d`，不因此改写各历史资料的验证日期。

当前正式版本为 [V1.2（构建 2026.09.13.3）](releases/v1.2/README.md)；上述历史核对起点保留用于追溯。

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

尚未实现的构想仍有价值；发布包存在不证明所有源码构建路径完整；文档日期不能独立决定其中的意图是否作废。维护时必须保留这些区别。

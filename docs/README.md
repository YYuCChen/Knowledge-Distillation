# 项目文档入口

本套文档区分当前能力、产品约定、未来探索和工程操作。编写核对基线是公开源码提交 `7f9c6a149e54b2e11ba1318a55a4efef185d9873`、公开 V1.1 构建 `2026.09.11.16`。后续任务应用前核对相关代码和发布状态。

| 问题 | 入口 |
|---|---|
| 当前产品做什么 | [产品范围](product/scope.md) |
| 哪些知识和用户数据边界必须保留 | [知识语义](product/semantics.md) |
| 界面修改如何保持用户意图 | [设计维护](product/design.md) |
| 代码职责在哪里 | [架构](engineering/architecture.md) |
| 怎样证明完成 | [测试与证据](engineering/testing.md) |
| 源码、构建和发行如何对应 | [发布](engineering/release.md) |
| 什么保留、什么清理 | [存储](engineering/storage.md) |
| 为什么分开管理 | [治理决策](decisions/0001-project-authority.md) |

尚未实现的构想仍有价值；发布包存在不证明所有源码构建路径完整；文档日期不能独立决定其中的意图是否作废。维护时必须保留这些区别。

# 项目文档入口

本文只链接仓库内已有的公开产品／工程文档。V3.0 源码与发行范围见 [项目 README](../README.md#v30-源码与发行范围)、[Wiki 工作流](engineering/wiki-workflow.md)、[来源支持合同](engineering/wiki-support.md) 和 [V3.0.1 补丁说明](releases/v3.0/release-20261009-3.0.1.md)、[V3.0 历史发行说明](releases/v3.0/release-20261009.md)；不引用仓库之外的私有施工材料。源码已接线不等于完成真实验收或正式发布。

本套文档区分当前能力、产品约定、未来探索和工程操作。编写核对基线是公开源码提交 `7f9c6a149e54b2e11ba1318a55a4efef185d9873`、公开 V1.1 构建 `2026.09.11.16`。后续任务应用前核对相关代码和发布状态。2026-09-12已合并Windows源码与治理文档；本次项目记忆归档核对起点为 `3d69b42d9b768ee559ec387faf149ef660adb53d`，不因此改写各历史资料的验证日期。

当前公开版为 [V3.0.1 更新页兼容修复（macOS 构建 2026.10.09.2）](releases/v3.0/release-20261009-3.0.1.md)，本地成品、固定标签资产回读和 Latest 核验通过，在线 V2 隔离握手与升级通过；[V3.0 raw → wiki 统一整理（构建 2026.10.09.1）](releases/v3.0/release-20261009.md)保留历史发行证据。受此缺陷影响的 V2.0／V3.0 更新页无法完成按钮流程；本次请下载独立安装器，由用户自行升级。后端 HTTP 验收不代表旧更新页可用；Windows 保留在 [V1.3](releases/v1.3/release-20260915.md)，本轮不发布 Windows V3.0。上述历史核对起点及 [Python 3.11 重做](releases/v1.2/python311.md)保留追溯，不能代替最新交付范围。

V3.0 当前源码采用 raw → wiki 统一整理及 Schema 27 的任务／接受收据，旧 SQL 知识保留历史只读；升级不自动重跑真实资料。获批的四项界面调整已接入源码，云端／本地决策配置只连接已有服务；真实模型能力与最终升级／发行验证分别记录。补丁构建 `2026.10.09.2` 仅面向 macOS Apple Silicon，Reddit／pyannote 已同意延期，Windows 不发 V3.0。下表二期与旧 roadmap 入口保留历史追溯，不覆盖这些当前范围。

| 问题 | 入口 |
|---|---|
| 当前产品做什么 | [产品范围](product/scope.md) |
| 哪些知识和用户数据边界必须保留 | [知识语义](product/semantics.md) |
| 界面修改如何保持用户意图 | [设计维护](product/design.md) |
| 代码职责在哪里 | [历史架构](engineering/architecture.md)、[当前 Wiki 工作流](engineering/wiki-workflow.md) |
| 工作纪律、怎样证明完成 | [测试与证据](engineering/testing.md) |
| Claude、Codex 怎样先讨论再交接开发 | [Agent 需求讨论、归档与开发交接](engineering/agent-collaboration.md) |
| Python共同基线与旧环境退役 | [运行环境契约](engineering/python-runtime.md) |
| 源码、构建和发行如何对应 | [发布](engineering/release.md) |
| 什么保留、什么清理 | [存储](engineering/storage.md) |
| 哪些后续优化已提出 | [版本优化待办](roadmap/next-version.md) |
| V3.0 当前实现与发行边界 | [Wiki 工作流](engineering/wiki-workflow.md)、[来源支持合同](engineering/wiki-support.md)、[V3.0.1 补丁说明](releases/v3.0/release-20261009-3.0.1.md)、[V3.0 历史发行说明](releases/v3.0/release-20261009.md) |
| V3.0 最初怎样规划 | [历史施工包](roadmap/v3-plan.md) |
| 远期构想和开放问题是什么 | [探索登记](roadmap/exploration.md) |
| 原始产品讨论和历史取舍在哪里 | [项目记忆归档](history/2026-09-project-memory/README.md) |
| 知识怎样走向实际应用 | [研究报告与证据](research/2026-09-11-knowledge-to-tools/report.md) |
| 项目资料怎样筛查、公开和留存 | [资料治理](engineering/project-memory.md) |
| 为什么分开管理 | [治理决策](decisions/0001-project-authority.md) |
| 二期当时按什么做 | [历史决策0004 三方对齐](decisions/0004-phase2-alignment.md) |
| 二期怎么施工、先做什么 | [二期施工计划](roadmap/phase2-plan.md) |
| 二期交付了什么、怎样正式收口 | [二期交付报告](releases/phase2/delivery.md)、[V2.0 正式发行](releases/v2.0/release-20260930.md) |
| 应用与知识库之间的接口 | [raw 接口规格](engineering/raw-interface.md) |
| wiki 侧的完整设计 | [个人知识系统方案](product/phase2-knowledge-system.md)、[vault-kit](../vault-kit/README.md) |
| 飞书随手记的需求 | [飞书随手记需求说明](roadmap/handoff-feishu-capture.md) |
| 哪些判断交给 Jev、没配置或出错时怎样 | [Jev 说明](engineering/jev.md) |
| 云端／本地决策的接口与概率语义 | [决策客户端合同（含独立模块阶段历史记录）](engineering/decision-model.md) |
| 二期的历史讨论与被部分取代的决策 | [二期讨论进展](roadmap/phase2-discussion.md)、[决策0002](decisions/0002-phase2-knowledge-backbone.md)、[决策0003](decisions/0003-material-foundation.md) |

所有任务先遵守 [AGENTS.md 最高工作原则](../AGENTS.md)：框架对、可用、面向未来即推进；可逆的放手迭代，不可逆的论证清楚。

尚未实现的构想仍有价值；发布包存在不证明所有源码构建路径完整；文档日期不能独立决定其中的意图是否作废。维护时必须保留这些区别。

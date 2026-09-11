> **历史研究归档。** 研究日期为2026-09-11，整理公开日期为2026-09-12。本次检查内容、归属和公开范围，未重新联网核实全部外部事实、复跑工具或复现论文。“本轮”均指原研究；建议未自动成为产品决定。

# 来源与证据清单

对应主文：[调研报告](report.md)  
核查日期：2026-09-11。GitHub 提交为本轮 API 读取到的 HEAD；不是依靠 Star 数排序。  
用途：便于回看和后续选取试验对象，不是安装清单。外部 SKILL.md 仅作为研究材料读取，没有执行其中指令。

## 1. GitHub：固定版本与阅读深度

### G01 · 仓颉：内容到方法能力包

项目：[kangarooking/cangjie-skill](https://github.com/kangarooking/cangjie-skill)  
固定提交：[`34e34bc5c7eb`](https://github.com/kangarooking/cangjie-skill/commit/34e34bc5c7eb1c7d6cae9f84a9bcc62acb2a971b)  
阅读范围：README、生成的纳瓦尔入口与判断训练卡、路由和 Token 基准、部分 issue。  
证据限制：存在实际产物；路由结果明确为静态自评，未证明宿主运行或业务收益。

- [README](https://github.com/kangarooking/cangjie-skill/blob/34e34bc5c7eb1c7d6cae9f84a9bcc62acb2a971b/README.md)
- [dist/naval-almanack-single/SKILL.md](https://github.com/kangarooking/cangjie-skill/blob/34e34bc5c7eb1c7d6cae9f84a9bcc62acb2a971b/dist/naval-almanack-single/SKILL.md)
- [dist/naval-almanack-single/references/capabilities/judgment-training.md](https://github.com/kangarooking/cangjie-skill/blob/34e34bc5c7eb1c7d6cae9f84a9bcc62acb2a971b/dist/naval-almanack-single/references/capabilities/judgment-training.md)
- [benchmarks/naval/phase1-routing-eval-50.md](https://github.com/kangarooking/cangjie-skill/blob/34e34bc5c7eb1c7d6cae9f84a9bcc62acb2a971b/benchmarks/naval/phase1-routing-eval-50.md)
- [benchmarks/naval/metrics-v2/benchmark.md](https://github.com/kangarooking/cangjie-skill/blob/34e34bc5c7eb1c7d6cae9f84a9bcc62acb2a971b/benchmarks/naval/metrics-v2/benchmark.md)

### G02 · 内容到任务方法包

项目：[gnipbao/content-to-skill](https://github.com/gnipbao/content-to-skill)  
固定提交：[`ce5776a51610`](https://github.com/gnipbao/content-to-skill/commit/ce5776a5161065836ed4647f9b96629d062ffdee)  
阅读范围：README、质量评估表、文件目录。  
证据限制：结构化流程规范；未独立复跑产物质量或实际任务效果。

- [README](https://github.com/gnipbao/content-to-skill/blob/ce5776a5161065836ed4647f9b96629d062ffdee/README.md)
- [references/evaluation-rubric.md](https://github.com/gnipbao/content-to-skill/blob/ce5776a5161065836ed4647f9b96629d062ffdee/references/evaluation-rubric.md)

### G03 · 博主内容与创作策略拆解

项目：[otter1101/blogger-distiller](https://github.com/otter1101/blogger-distiller)  
固定提交：[`167f21baadcb`](https://github.com/otter1101/blogger-distiller/commit/167f21baadcb4b614a9a25268df8ca1e23e48ae7)  
阅读范围：README、文件目录。  
证据限制：重点偏内容创作打法；口播转写可选，不能等同完整专业视频蒸馏。

- [README](https://github.com/otter1101/blogger-distiller/blob/167f21baadcb4b614a9a25268df8ca1e23e48ae7/README.md)


### G04 · 博主打法与迁移验证

项目：[LearnPrompt/paoding-skill](https://github.com/LearnPrompt/paoding-skill)  
固定提交：[`44a7c2b36bd7`](https://github.com/LearnPrompt/paoding-skill/commit/44a7c2b36bd70d9e71a695251a536370c43fba9c)  
阅读范围：README、证据与验证细则、目录。  
证据限制：有反例、独立留出和同题对照规则；不是已发布的流量提升实验。

- [README](https://github.com/LearnPrompt/paoding-skill/blob/44a7c2b36bd70d9e71a695251a536370c43fba9c/README.md)
- [skills/paoding/references/证据与验证.md](https://github.com/LearnPrompt/paoding-skill/blob/44a7c2b36bd70d9e71a695251a536370c43fba9c/skills/paoding/references/证据与验证.md)

### G05 · 教程到操作技能

项目：[brenoepics/video-to-skill](https://github.com/brenoepics/video-to-skill)  
固定提交：[`c527aa9b64cd`](https://github.com/brenoepics/video-to-skill/commit/c527aa9b64cdb7e39a9d20dae6218c7cfc513473)  
阅读范围：README、验证协议、BENCHMARK、实现与测试目录。  
证据限制：验证协议可读；性能基准使用合成音视频，不能证明生成任务成功率。

- [README](https://github.com/brenoepics/video-to-skill/blob/c527aa9b64cdb7e39a9d20dae6218c7cfc513473/README.md)
- [references/verification-protocol.md](https://github.com/brenoepics/video-to-skill/blob/c527aa9b64cdb7e39a9d20dae6218c7cfc513473/references/verification-protocol.md)
- [BENCHMARK.md](https://github.com/brenoepics/video-to-skill/blob/c527aa9b64cdb7e39a9d20dae6218c7cfc513473/BENCHMARK.md)

### G06 · 营销工作的实际消费端

项目：[coreyhaines31/marketingskills](https://github.com/coreyhaines31/marketingskills)  
固定提交：[`5b2c0007766c`](https://github.com/coreyhaines31/marketingskills/commit/5b2c0007766c6a1cf1d53fd8fc73e979e0821022)  
阅读范围：README、CRO、产品背景 Skill、CRO 测试样本。  
证据限制：实际任务指令和预期输出可读；测试覆盖不等于转化率提升。

- [README](https://github.com/coreyhaines31/marketingskills/blob/5b2c0007766c6a1cf1d53fd8fc73e979e0821022/README.md)
- [skills/product-marketing/SKILL.md](https://github.com/coreyhaines31/marketingskills/blob/5b2c0007766c6a1cf1d53fd8fc73e979e0821022/skills/product-marketing/SKILL.md)
- [skills/cro/SKILL.md](https://github.com/coreyhaines31/marketingskills/blob/5b2c0007766c6a1cf1d53fd8fc73e979e0821022/skills/cro/SKILL.md)
- [skills/cro/evals/evals.json](https://github.com/coreyhaines31/marketingskills/blob/5b2c0007766c6a1cf1d53fd8fc73e979e0821022/skills/cro/evals/evals.json)

### G07 · 内容到练习与交付

项目：[michalparkola/tapestry-skills](https://github.com/michalparkola/tapestry-skills)  
固定提交：[`80e1dc56df74`](https://github.com/michalparkola/tapestry-skills/commit/80e1dc56df74d1cb849ad649c7ead9756e7929bb)  
阅读范围：README、Ship-Learn-Next 实际 Skill。  
证据限制：展示既有方法消费不同内容的路径；无独立长期学习效果数据。

- [README](https://github.com/michalparkola/tapestry-skills/blob/80e1dc56df74d1cb849ad649c7ead9756e7929bb/README.md)
- [ship-learn-next/SKILL.md](https://github.com/michalparkola/tapestry-skills/blob/80e1dc56df74d1cb849ad649c7ead9756e7929bb/ship-learn-next/SKILL.md)

### G08 · 教材方法进入翻译生产

项目：[3060226349kk-cmd/en-zh-max](https://github.com/3060226349kk-cmd/en-zh-max)  
固定提交：[`1675cbe1e814`](https://github.com/3060226349kk-cmd/en-zh-max/commit/1675cbe1e814551c89d958981b2598360c8fdb05)  
阅读范围：README、项目目录。  
证据限制：教材来源与技能数量为作者陈述；未逐页核验教材或复跑翻译质量测试。

- [README](https://github.com/3060226349kk-cmd/en-zh-max/blob/1675cbe1e814551c89d958981b2598360c8fdb05/README.md)


### G09 · 本地知识按需取用

项目：[tobi/qmd](https://github.com/tobi/qmd)  
固定提交：[`04e4dbd8245c`](https://github.com/tobi/qmd/commit/04e4dbd8245c527a88f1a8f0bda547aef9ca81fb)  
阅读范围：README。  
证据限制：检索技术与 Agent 接口的参照，不证明来源正确或最终答案正确。

- [README](https://github.com/tobi/qmd/blob/04e4dbd8245c527a88f1a8f0bda547aef9ca81fb/README.md)


### G10 · 可复用内容处理模式

项目：[danielmiessler/Fabric](https://github.com/danielmiessler/Fabric)  
固定提交：[`b682dad740f2`](https://github.com/danielmiessler/Fabric/commit/b682dad740f24e85ce9a48d23babc6780dd476ac)  
阅读范围：README。  
证据限制：作为任务模式复用的路线参照，未验证具体模式效果。

- [README](https://github.com/danielmiessler/Fabric/blob/b682dad740f24e85ce9a48d23babc6780dd476ac/README.md)


### G11 · 认知视角与人格化顾问

项目：[superpilot69/vida-open-archive](https://github.com/superpilot69/vida-open-archive)  
固定提交：[`8b39b16f5db7`](https://github.com/superpilot69/vida-open-archive/commit/8b39b16f5db7fd1a253f9337362f3dcb9b864025)  
阅读范围：README、实际 vida-perspective Skill、目录。  
证据限制：可见认知模型与角色扮演要求；不等同真人立场、能力或授权。

- [README](https://github.com/superpilot69/vida-open-archive/blob/8b39b16f5db7fd1a253f9337362f3dcb9b864025/README.md)
- [skills/vida-perspective/SKILL.md](https://github.com/superpilot69/vida-open-archive/blob/8b39b16f5db7fd1a253f9337362f3dcb9b864025/skills/vida-perspective/SKILL.md)

### G12 · 工作经验到可复用方法

项目：[tigerless-labs/autoharness](https://github.com/tigerless-labs/autoharness)  
固定提交：[`11d7b3791870`](https://github.com/tigerless-labs/autoharness/commit/11d7b379187041ef353bce03fffaf994545b489b)  
阅读范围：README、目录。  
证据限制：使用／调用信号不是独立任务质量；README 引用的外部研究不是本项目成效。

- [README](https://github.com/tigerless-labs/autoharness/blob/11d7b379187041ef353bce03fffaf994545b489b/README.md)


### G13 · 运营框架的证据风险样本

项目：[Formangarden524/nuwa-distilled-skills](https://github.com/Formangarden524/nuwa-distilled-skills)  
固定提交：[`f56d76ed9774`](https://github.com/Formangarden524/nuwa-distilled-skills/commit/f56d76ed9774f76d54a00474e4edcf7c1781483a)  
阅读范围：实际小红书运营 SKILL.md、目录。  
证据限制：部分精确规则未取得可核对原始依据；不将其当成平台事实。

- [README](https://github.com/Formangarden524/nuwa-distilled-skills/blob/f56d76ed9774f76d54a00474e4edcf7c1781483a/README.md)
- [xiaohongshu-ops-framework/SKILL.md](https://github.com/Formangarden524/nuwa-distilled-skills/blob/f56d76ed9774f76d54a00474e4edcf7c1781483a/xiaohongshu-ops-framework/SKILL.md)

## 2. GitHub 用户反馈：单独于项目宣传

| 编号 | 来源 | 本轮观察 | 不能据此声称 |
|---|---|---|---|
| I01 | [仓颉 #14：希望有执行步骤／视频](https://github.com/kangarooking/cangjie-skill/issues/14) | 有使用说明与演示需求 | 所有用户都不会使用 |
| I02 | [仓颉 #13：询问 Codex 支持](https://github.com/kangarooking/cangjie-skill/issues/13) | 有宿主选择与安装理解成本 | 当前版本不支持 Codex |
| I03 | [仓颉 #20：技能目录负担](https://github.com/kangarooking/cangjie-skill/issues/20) | 提交者报告大量技能带来的上下文问题，并推广自有方案 | 其数字适用于全部宿主，或方案已独立验证 |
| I04 | [仓颉 #29：缺少依赖导致自检不可达](https://github.com/kangarooking/cangjie-skill/issues/29) | 提交者报告具体环境与修补方法 | 本轮已经复现／替项目修复 |
| I05 | [仓颉 #30：引用断链被拦截](https://github.com/kangarooking/cangjie-skill/issues/30) | 提交者报告发布校验拦下错误产物 | 方法语义质量因此得到保证 |

Issue 是公开反馈，可能过时、存在环境差异，也可能含宣传。无关广告已排除；未执行帖子中的补丁或命令。

## 3. OPC / FDE 公开社区与实践材料

### C01 · OneOPC 公开入口

来源：[OneOPC](https://www.oneopc.ai/)  
阅读：公开首页、知识与实践入口说明。  
用途：确认该社区的知识库／实践圈定位。  
限制：未进入其知识星球，未把内部实战记录列为证据；不据此代表所有 OPC 社区。

### C02 · FDE 落地公开入口

来源：[FDE 落地](https://www.fde.pub/)  
阅读：公开能力及业务服务菜单。  
用途：观察从能力到场景、交付和业务方案的组织方式。  
限制：菜单不是实施成果证明。

### C03 · FDE 中国社区运营日报

来源：[运营日报 Skill](https://fdechina.ai/skills/ai-operations-daily)  
阅读：任务输入、交付材料、风险、示意案例。  
用途：具体任务的输入／输出约定。  
限制：案例标有“示意”；网页链接的 `fdeclub/fde-skills` 源码本轮返回不可取得，未阅读源码。本文不将示意结果写成实际成果。

### C04 · 选品 Skill 实践

来源：[瀚海方舟：搭建 Skill 的“产品平衡法则”，53AI 转载](https://www.53ai.com/news/AIdianshang/2026070109475.html)  
社区入口：[FDE 中国社区转载](https://fdechina.ai/cases/insights/ecom-2026070109475)  
发布标记：2026-07-01（53AI 页面）。  
阅读：语义拆需求、脚本采集、AI 分析、人工选择、报告／Excel 输出。  
限制：为同一文章传播链，不算两份独立证据；作者原公众号全文链接未独立取得，没有可重复经营收益测试。

### C05 · 高价率运营工作台

来源：[FDE 中国社区：高价率运营 AI 工作台](https://fdechina.ai/cases/insights/ecom-ai-workbench)  
阅读：可取得的文章文本，尤其评测、金标、线上问题回流、建议归因、日志缺口。  
用途：展示业务 Skill 的评测与维护为何比文件生成更难。  
限制：社区文章自述，未获取生产代码、运行记录或独立业务数据；不对作者身份或文章全部业务数字提供独立背书。页面同时出现登录提示，本报告只依据实际返回且已读的文字，没有付费、登录或绕过访问控制。

## 4. 社交平台：仅作为实践线索／需求信号

| 编号 | 来源 | 使用方式与限制 |
|---|---|---|
| S01 | [小红书：AI科技猎人，蒸馏要能解决问题](https://www.xiaohongshu.com/explore/6a79c603000000000502a04e) | 已读正文；认知方法用于问题分析的主张，不是效果实验 |
| S02 | [小红书：LUCKYLEE，北美销售负责人 Skill](https://www.xiaohongshu.com/explore/6a1a50e8000000003501dd2d) | 已读正文；判断规则提炼的线索，没有公开完整产物与销售对照 |
| S03 | [X：仓颉作者谈在线使用入口](https://x.com/i/status/2080540561870565525) | 作者自述；用于理解从文件到可用界面的落差，不核定商业回报 |
| S04 | [X：content-to-skill 分享](https://x.com/i/status/2073795959147131013) | 用于发现仓库；项目内容以 GitHub 直接读取为准 |
| S05 | [X：蒸馏内容与电商工具实践自述](https://x.com/i/status/2086751396549083311) | 帖子同时提到多个项目，不据此推断某认知 Skill 导致电商收益；未作为主文成效证据 |
| S06 | [Reddit：SkillsBench 作者帖](https://www.reddit.com/r/ClaudeAI/comments/1r7jb7k/we_build_skillsbench_the_first_benchmarks_that/) | 已读帖文；与论文同源，不视为独立复现或社区共识 |

小红书读取时使用搜索结果提供的可访问链接；报告保留不含访问 token 的公开作品地址。平台可能要求登录或日后改变可见性。未发布帖子、评论、私信或加入社群。

## 5. 官方机制与研究

### R01 · OpenAI 官方 Skill 文档

[Build skills](https://developers.openai.com/codex/skills/)  
阅读：页面正文，特别是包结构、显式／隐式调用、渐进加载。  
支持的结论：Skill 是任务方法与资源的运行时载体，不是把专家知识训练进模型参数。  
不支持的结论：任何自动生成的 Skill 都可靠；所有宿主安装方式相同。主文未提供安装步骤，避免混淆产品版本和用户环境。

### R02 · SkillsBench v4

[arXiv v4 摘要](https://arxiv.org/abs/2602.12670v4)  
本轮读取范围：版本信息与摘要；没有重跑基准。  
摘要口径：87 任务、18 组配置，33.9% → 50.5%，+16.6 个百分点。  
限制：特定基准及匹配设置，不能当成电商或本产品收益。不同版本、网站当前结果、社区旧帖不可混算。

### R03 · SkillsBench v1

[arXiv v1 摘要](https://arxiv.org/abs/2602.12670v1)  
读取范围：版本信息与摘要；HTML 全文抓取返回异常内容，未把它当成成功阅读全文。  
摘要口径：86 任务、11 领域、7 组配置、7,308 条轨迹；人工整理方法平均增益与自生成方法结果属于这一版设置。  
限制：不能将自生成实验外推成所有源材料蒸馏都无效。本文不就未核对全文的具体提示设置做断言。

### R04 · SkillAxe v2

[arXiv v2](https://arxiv.org/abs/2606.10546v2)  
读取范围：摘要、版本及研究说明。  
支持的线索：从质量影响、触发、遵循、覆盖等方面诊断和改进方法。  
限制：预印本作者报告，未独立复现；主文不将其效果数字用于业务承诺。

### R05 · SkillGLoW v1

[arXiv v1](https://arxiv.org/abs/2609.02217v1)  
读取范围：摘要与提交信息，v1 为 2026-09-02。  
支持的线索：把任务经验归并为可迁移程序，实例细节与稳定流程分开，以执行结果控制改动。  
限制：新预印本，非电商博主蒸馏实验；本轮未做全文方法审计或复现。

## 6. 检索与筛选说明

检索围绕三组问题，而非仅搜索“知识蒸馏器”：

1. **生产端**：content/book/video/blogger to skill、知识蒸馏、博主蒸馏、方法论提取。
2. **消费端**：marketing skills、运营诊断、翻译、学习行动、知识检索、个人经验复盘。
3. **验证端**：skills benchmark、generated skills evaluation、held-out、真实应用反馈、OPC/FDE 落地。

GitHub 用项目检索、仓库 API、文件和 issue 直接读取；社区用公开网页与可用平台检索。搜索服务一度限流后改用其他公开搜索渠道，没有购买额度。仅将成功读到的内容纳入主文。

筛选优先级是：

- 是否能看到实际产物、输入与输出，而不只是宣传。
- 是否覆盖“新任务里怎么用”，而非只描述如何生成。
- 是否有验证方法、反例、失败与限制。
- 是否为独立来源，还是同一文章／作者的重复传播。

未采纳的典型结果：二手课程售卖页、只谈“复刻大脑”的流量文章、无任务或产物的 Skill 列表、无关广告、无法确定来源的收益数字。

## 7. 与本地项目的关系

本轮对照了工程目录的当前上下文、权威索引及产品规格中有关远期方向的约定。调研不改变既有 Topic 的组织边界，不自动恢复已取消的合成能力，不创建用户认知画像，不增设正式知识类型，也不推进代码施工。

两份报告研究完成时保存在独立调研目录，未修改产品契约；2026-09-12 经用户授权整理进入公开仓库。后续若选择试验，应另行明确场景、数据、产物与验收。

本报告引用原文时以短摘录／转述为主，没有把第三方全文、书籍或视频转录重新打包交付。

# 调研 C：个人知识图谱与个人数据建模（2026-09-29）

说明：以下均来自本次实际抓取的页面摘要；Solid、FOAF、McAdams 原文（PubMed）未能抓取成功，故未写入细节。"借用点"是我的推断，已与"查到的事实"分开标注。

## 1. 个人知识图谱（PKG）
查到：
- Balog & Kenter 2019 定义：PKG 是关于实体及其关系的结构化知识，这些实体和关系"对个人重要，而非对一般人重要"。三个特征：以用户为中心；蜘蛛网结构（每个实体直接或间接连到用户）；与外部知识库集成并去重。来源：https://www.tomkenter.nl/pdf/Personal%20Knowledge%20Graphs%20-%20ICTIR%202019.pdf
- 难点：稀疏、短暂的关系如何表示；长尾实体（朋友、私人物品）没有外部文档，难以链接；必须自动填充和维护，没有编辑审核，只能靠上下文推断；与外部平台的同步和隐私控制。同上。
- 2023 生态综述（Ecosystem for PKG）：PKG 强调个人拥有完整读写权。语句类型包括客观事实、个人事件记录（如"5月1日看牙医"）、访问日志、信念与概率（"我认为……"）、他人观点（"我妈认为……"）。元数据（出处、时间、置信度）是正确使用的关键。数据源分私有（日历、邮件）、公开、开放关联数据（Wikidata）。开放难点：词表标准化、冲突事实处理、非技术用户管理、同步回非结构化源、数据可靠性。来源：https://arxiv.org/pdf/2304.09572
- 借用点（推断）：
  - 以"我"为中心，所有页面都要能一跳/多跳连回"我"。
  - 每条事实带 出处/时间/置信度。
  - "他人观点"和"我的信念"应与客观事实分开标注。
  - 长尾实体（人、物）不必强求外链。

## 2. 生活日志（MyLifeBits / Memex）
查到：
- MyLifeBits 始于 2001 年，受 Bush 的 Memex 启发，目标是"收集 Bell 一生的存储"。功能：全文搜索、文字/语音注释、超链接。到 2016 年 Bell 放弃了可穿戴相机，因为智能手机已基本实现 Memex 愿景。来源：https://en.wikipedia.org/wiki/MyLifeBits
- IEEE Spectrum 报道：自动抓拍和多源整合有效（可检索数月、数年前的细节）。但最初界面像电子表格，80GB+ 数据难以浏览；数据量造成索引和注释困难；存在隐私问题。来源：https://spectrum.ieee.org/total-recall
- Memex 的"trails"：用户在任意资料之间建立个人化路径，可加评论、分支、分享；Bush 批评僵硬的层级索引，主张联想式链接。来源：https://en.wikipedia.org/wiki/Memex
- 借用点（推断）：全量捕获的瓶颈在检索与组织，不在存储。因此应保留"精选 + 链接 + 注释"，用主题/叙事线（trail）串联，而不是堆原始记录。

## 3. 可复用本体
查到：
- schema.org/Person 有：knows、knowsAbout、knowsLanguage、skills、birthDate、hasOccupation、jobTitle、worksFor、alumniOf、homeLocation、workLocation、owns、seeks、makesOffer、家庭关系等。但缺少显式的目标和偏好字段，仅有 seeks/makesOffer 近似。来源：https://schema.org/Person
- schema.org 的 Person/Event 只有搜索结果标题，未读到 Event 页面内容；Solid、FOAF 页面抓取失败，本次不下结论。
- 借用点（推断）：schema.org 的字段名可作为 frontmatter 命名参考；目标、偏好、现状需要自建字段。

## 4. 人格/传记分层（McAdams）
查到：
- 三层：(1) 性格特质（Big Five 类，稳定）；(2) 特征性适应（情境化的动机、发展关切、人生策略）；(3) 叙事认同（内化、不断演化的自我故事，整合过去、现在和想象的未来）。三层同时看才完整。来源：https://en.wikipedia.org/wiki/Narrative_identity
- 叙事的结构要素：时间连贯、因果连贯、主题连贯；主题：救赎（redemption）、污染（contamination）、能动性（agency）、共融性（communion）。同上。
- 借用点（推断）：
  - 层 1 = 稳定倾向页（低频更新）。
  - 层 2 = 目标/关切/偏好/现状页（中频更新，带有效期）。
  - 层 3 = 人生叙事、转折点事件、意义解释页（用户自己的说法，应标注为"叙事"而非事实）。
  - 三层更新频率和证据标准不同，应分开存放。

## 5. 事件与时间建模
查到：
- 有效时间（valid time）：事实在现实中为真的时期；事务时间（transaction time）：数据被录入系统的时间。双时态模型同时保留两者，例：搬家发生日与记录日不同。来源：https://en.wikipedia.org/wiki/Valid_time
- 面向 AI 记忆的说明：事后到达的来源（邮件、转录）、更正、批量回填都会使事件日期与录入日期分离；无双时态时，"迟到的更正要么篡改历史，要么日期错误"；可以回答"某日期时系统相信什么"。来源：https://www.past.dev/guides/bitemporal-data
- PKG 综述里的"个人事件记录"与"信念、他人观点"也强调时间与置信度元数据（见第 1 节）。
- 借用点（推断）：
  - 状态类事实（住址、职位、在做的项目）用 valid_from / valid_to。
  - 事件类事实用单个发生日期。
  - 另加 recorded_at（录入时间）和 source。
  - 更正时不覆盖，而是关闭旧记录的有效期并新增。

## 已知难点汇总
- 长尾个人实体缺少外部锚点；无人工编辑，需靠 LLM 自动维护，冲突事实需要处理规则。
- 全量记录的实际瓶颈是检索、注释和隐私。
- 现成本体缺少"目标、偏好、现状"，需自建。

## 未查到 / 待补
- Solid、FOAF、schema.org/Event 的具体内容；McAdams & Pals 2006 原文；PKG 综述里具体的实体类型清单；轻量的目标/偏好本体。

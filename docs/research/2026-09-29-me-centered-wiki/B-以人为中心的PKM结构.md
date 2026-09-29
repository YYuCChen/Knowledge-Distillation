# 调研B：以人为中心的PKM组织结构（2026-09-29）
说明：部分官方页面（fortelabs、Matuschak原页）抓取被拦，PARA定义取自二手来源；ACCESS各文件夹含义未能核实（仅见 https://twitter.com/NickMilo/status/1530162446459019265 标题），不写。

## 1. PARA
- 顶层：Projects（有终点的目标）/Areas（长期责任，无终点）/Resources（主题兴趣）/Archives（完成或不活跃）。维度=行动性，"按多可行动而非按信息类型"；税单放"Taxes 2024"项目而非通用"Invoices"。https://web-highlights.com/blog/master-your-second-brain-part-1-how-to-use-the-para-method/
- 批评：只是"往桶里塞"、缺少知识互联；搭建与维护耗时；文件夹被认为过时、搜索可替代；术语偏个人生产力、隐含GTD前置知识。https://medium.com/design-bootcamp/para-method-review-does-everyone-really-love-the-organizing-method-c7d1b1bb5ed7
- 推论（我的判断）：Projects/Archives 随时间状态变化，条目需不断搬家，是腐烂点；Areas 相对稳定。

## 2. Nick Milo ACE / MOC
- ACE 三个顶层：Atlas（知识，按相关性/空间）、Calendar（时间，回顾）、Efforts（行动，按优先级）；强调"是三种心智空间而非仅文件夹"，"结构要靠挣得（structure must be earned）"，先用基础版再演化。https://blog.linkingyourthinking.com/notes/ace-folder-framework
- Efforts 状态：On / Ongoing / Simmering / Sleeping（看板式"拉取"）。https://tfthacker.substack.com/p/ace-an-exciting-framework-for-pkm
- Atlas 内含知识卡、MOC、来源；Calendar 含日记、复盘；MOC=主题索引，一条笔记可在多个MOC；约"同主题5+条笔记开始混乱"时才建。https://yu-wenhao.com/en/blog/lyt-framework-guide/
- Efforts 优于 Projects：项目"过早施加过多结构"，effort 范围可弹性伸缩。https://hannahswainlovik.eu/2024/12/02/rethinking-work-efforts-matter-more-than-projects/
- ACCESS（Atlas/Calendar/Cards/Extras/Sources/Spaces）：未找到可核实的正文。

## 3. Johnny.Decimal / Zettelkasten / Evergreen
- JD：最多10个Area、每个最多10个Category，ID如12.03；类别以下不再建文件夹；须维护索引；"两次点击可达"。局限：10/10硬约束对复杂信息偏僵。https://www.dsebastien.net/2022-04-29-johnny-decimal/ ；官方文档强调"类别是最重要概念"，宁少而宽。https://johnnydecimal.com/documentation/areas-and-categories
- 卢曼：两个箱（文献箱/主箱），主箱卡片用自己的话、自足；分支编号1、1a、1a1，"反对按主题层级排序，用固定位置"；箱I约2.3万卡，箱II约6.6万卡；靠交叉引用与枢纽笔记连接。https://www.ernestchiang.com/en/posts/2025/niklas-luhmann-original-zettelkasten-method/
- Zettelkasten 批评：规模大后维护越来越耗时、过度链接产生噪音；只对有特定主题目标者有用；"收藏家谬误"。https://forum.obsidian.md/t/mocs-vs-zettelkasten-an-80-20-approach-for-those-of-us-who-arent-luhmann/106518 ；https://fricklr.com/en/rant-why-the-card-box-method-with-obsidian-is-rubbish/
- Evergreen notes：原子、概念导向、密集链接、联想式而非层级、为自己而写；问"连到哪些想法"而非"放哪个文件夹"。https://notes.andymatuschak.org/z5E5QawiXCMbtNtupvxeoEX
- 共识：Zettel与MOC互补，80/20 取"原子笔记质量+策略性MOC"，不追求链接密度（同上forum链接）。

## 4. 时间主轴 / Life OS
- 时间层级 年→季→月→周→日；长周期偏目标，短周期偏任务/习惯；主题（PARA）与时间两套系统并存，用主题标签把任务串到各周期。https://obsidian-life-os.pages.dev/guide/beginner-guide/core-concept
- PIOS 实例：年记约25%规划/75%反思，季记做"12周年"，月记兼日志与消费追踪；作者自认周记是最弱一层，靠嵌入日记维持。https://www.polyinnovator.space/how-i-manage-yearly-quarterly-monthly-weekly-daily-notes-in-obsidian/
- Bullet journal：Index + Future log + Monthly log + Daily log（+Key）。https://en.wikipedia.org/wiki/Bullet_journal
- interstitial journaling：本轮未查到可核实来源，不写。

## 5. 人物页 / 决策日志 / 自我手册
- 个人CRM：人物页 frontmatter 如 prm-tier / prm-last-contacted / prm-cadence；联系历史不手填，而从带日期笔记中的 [[人名]] 链接推导。https://github.com/xuvi7/obsidian-personal-crm
- 批评：需要"工作流之外的额外动作"，忙时被跳过，数据过时后失去信任。https://rarefriend.com/blog/obsidian-personal-crm-why-it-fails
- 决策日志（Farnam Street）：情境、问题框架、变量、备选及否决理由、结果区间、带概率的预期、当时身心状态；事后对照，防后见之明。https://fs.blog/decision-journal/
- Manual of Me：说明你如何工作、偏好、需求、动机、价值、优势；具体章节官网未列。https://www.manualof.me/

## 稳定 vs 易腐（归纳）
- 耐用：少量顶层（ACE 3个/PARA 4个）；时间轴日记（追加式，无需搬家）；MOC/索引；从日期笔记推导而非手填；原子笔记以概念命名。
- 易腐：按状态搬家的文件夹（Projects/Archives）、人工维护的"最后联系"字段、过深层级与过密链接、周记类中间层。

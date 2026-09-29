# 调研 A：LLM 维护 wiki 与 AI 记忆系统中的"用户/自我"结构（2026-09-29）

说明：仅记录实际读到的内容。Letta archival、Zep 论文原文、Claude 帮助中心页面因抓取受限未读到全文，相关处已标注。

## 1. Karpathy LLM Wiki 及扩展
- 三层：raw sources（不可变）→ wiki（LLM 全权维护的 md）→ schema（约定结构与流程的文档）。三个操作：ingest / query（有价值的回答回写成新页）/ lint（查矛盾、过期、孤儿页、缺失交叉引用）。特殊文件：index.md（按类别的内容目录）、log.md（只追加的时间日志）。人负责选源和提问，LLM 负责维护。https://gist.github.com/karpathy/442a6bf555914893e9891c11519de94f
- 原 gist 不规定页面类型，由 schema 按领域自定。（未见针对"关于自己"的官方版本。）
- 评论区经验（gist 讨论摘要）：结构要强制而非建议，否则各处形成"方言"；写入时解决冲突比事后 lint 便宜；保留被取代的论断而非删除；记录文档顺序、有效时间和"信念时间"；采集失败要显式，静默丢数据比 ingest 失败更糟。同上链接。
- rohitg00 LLM Wiki v2：记忆分四层（working / episodic / semantic / procedural），越往后越浓缩持久；实体类型化（人、项目、概念、决策），关系带语义（uses/contradicts/caused）；confidence（来源数、新近度、是否被反驳）；supersession（新旧显式链接，旧条目保留但标 stale）；遗忘曲线只降权不删除。https://gist.github.com/rohitg00/2067ab416f7bbe447c1977edaaa681e2
- v2 的批评（同页摘要）：数值置信度有虚假精确；LLM 自动写入会悄悄污染库，需人工闸门；遗忘曲线会丢掉解释"为何如此"的历史背景。
- v3（HousamKak）：分证据（不可变）/ 观察（LLM 解读）/ 信念状态 / 渲染页；状态变化记为只追加的转移记录；置信度拆成多维；不同类型的断言用不同衰减速度。批评 v1：wiki 自身成为唯一真相源时，摘要失真会悄悄累积。https://gist.github.com/HousamKak/ba96124547d1b7c68d270c293106fe53
- 实现参考：NicholasSpisak/second-brain（Obsidian，skills：setup/ingest/query/lint，"LLM 是图书管理员，你是策展人"）https://github.com/NicholasSpisak/second-brain 。均面向文章/研究资料，未见专门的"个人自我"实现。

## 2. Agent 记忆框架对"用户"的建模
- Letta/MemGPT：core memory 是常驻上下文的"记忆块"，典型为 human（用户信息）与 persona（agent 自身）；每块有 label、description、value、字符上限；可设只读；多 agent 共享时是 last-write-wins，建议只读或受控写。agent 自己编辑块。https://docs.letta.com/guides/agents/memory-blocks/ 。archival memory（向量库、按需检索）：只确认存在，https://docs.letta.com/guides/core-concepts/memory/archival-memory 未读到细节。
- Zep/Graphiti：时间知识图谱，双时间轴：valid time（事实在现实中何时成立/失效）与 transaction time（系统何时得知/失效）。新事实与旧事实矛盾时旧边被标失效而非删除；可查"2023 年谁是 CEO"及"3 月 1 日时我们知道什么"；能处理迟到信息。https://mintlify.wiki/getzep/graphiti/concepts/temporal-model
- Mem0：SQL（事实与元数据，权威记录）+ 向量 + 实体库三存储；自动抽取是增量的（"从 Austin 搬到 Seattle"会新增而不静默改写旧条），纠正/删除要显式 update/delete。https://docs.mem0.ai/core-concepts/memory-types
- LangMem：语义记忆分 profile（单文档，更新覆盖，适合"当前状态"）与 collection（多文档，需在新增与删除/失效/合并之间调和）；另有 episodic（成功范例）、procedural（行为规则，从 prompt 演化）；写入时机分热路径与后台反思，后台召回更高。https://github.com/langchain-ai/langmem/blob/main/docs/docs/concepts/conceptual_guide.md
- ChatGPT（逆向工程，非官方）：四层——session metadata（不持久）、User Memory（显式长期事实，作者见到 33 条：姓名、年龄、职业目标）、近期对话摘要（片段而非全文）、当前会话窗口；用户可说"记住/删除"。作者认为不必用 RAG。https://manthanguptaa.in/posts/chatgpt_memory/
- Claude Code（官方文档）：CLAUDE.md（人写：指令与规则）与 auto memory（Claude 写：学到的偏好与纠正）明确分工；auto memory 四类 type：user（角色、专长、偏好）/ feedback / project / reference；MEMORY.md 为一行一条的索引，仅前 200 行或 25KB 每次加载，细节放主题文件按需读；文件 frontmatter 自动记录 modified 时间戳"表明事实新鲜度"；建议 CLAUDE.md 小于 200 行，互相矛盾的规则会被随意取舍，需定期清理过期内容；Claude 不记能从代码推出的东西。https://code.claude.com/docs/en/memory
- 失败案例：Mem0 用户审计 10,134 条生产记忆，97.8% 为垃圾（启动提示词反复重抽取 52.7%、系统噪声、模型编造用户画像 5.2%、召回内容又被当新事实重抽取的反馈回路）；换更强模型反而抽取更不加区分；建议加质量闸门、负例、REJECT 动作、标记已召回内容不再抽取、区分人与 AI。https://github.com/mem0ai/mem0/issues/4573

## 3. 公开的"给 AI 读的个人档案"结构
- nlwhittemore/personal-context-portfolio：10 个 md——identity、role-and-responsibilities、current-projects、team-and-relationships、tools-and-systems、communication-style、goals-and-priorities、preferences-and-constraints、domain-knowledge、decision-log；由本人所有，可用 AI 起草初稿，以访谈协议生成；"活文档"，项目文件随项目更新、优先级按季度变。https://github.com/nlwhittemore/personal-context-portfolio （偏职场，缺"经历/所思/健康生活"类。）
- TheCYPER/MyContext-hackathon：md+YAML 存独立 git 仓库；带类型关系（谁参与项目、哪条笔记支撑论断、什么取代了旧结论）与证据字段；新信息默认等待审核才入库，避免后台自动采集；AI 负责检索与提出更新，看板只读。https://github.com/TheCYPER/MyContext-hackathon
- 其他同类仓库仅见标题，未读：Ayush7614/personal-context、vitaecontext/vitaecontext、aleen42/PersonalContext。

## 跨项提炼（供设计参考，属推断）
- 分层共识：常驻小块（身份/当前状态/偏好，约 200 行内）+ 按需检索的主题页 + 只追加的事件/日志层。
- 时间处理共识：不删旧事实，标失效并链接到新事实；区分"事实成立时间"与"记录时间"；当前状态用可覆盖的 profile 页，历史用带日期条目。
- 谁写：规则/身份由人写或人审；观察类由 AI 写并带来源与时间；入库前有闸门。
- 教训：无差别抽取有害；schema 需强制；wiki 不应是唯一真相源，应保留指向原始证据的链接。

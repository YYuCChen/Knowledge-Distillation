# R06 纯多链接候选合同

本模块尚未接入产品，不能把候选有效或同题 eligible 当作来源采集成功。只新增 multi_link_candidates.py 及合成测试，不修改 intake、handler、Store/schema、collections、飞书卡片、raw 编号或依赖锁。

`prepare_multi_link_input(LinkInput, explicit_same_topic=False)` 返回冻结的 `MultiLinkCandidates`。LinkInput 的 namespace/version 是调用方提供的原稳定身份格式，raw_utf8 是完整原 bytes，sha256 须精确匹配；markdown 和 feishu_post 分别读取原文本与结构 JSON。准入失败仍保留同一个 source 对象及全部 bytes、diagnostics，没有局部值伪装通过。hash 是一致性检查，不证明元数据来自认证消息；重复调用不是数据库 CAS 或持久幂等。

发生顺序采用零基 position，invalid/ambiguous 不使后项提前。Markdown 使用已锁 `markdown-it-py==4.2.0` 的 CommonMark 完整 parse，读取 inline.children 中真实 link_open href 与 label token；Evidence.line_range 是 block token.map 的零基、尾不含行区间。没有 char/byte offset，没有 rule wrapper、monkeypatch、二次 parseInline 或 HTML render。解析 token 会解实体/转义，token 值不冒称原字节切片；原 bytes 才是完整正本。reference href 是上游实际解析值，证据仅使用位置所在 block 行区间，未额外声称定义精确位置。

裸 URL 只识别普通 text tokens；使用上游 parseLinkDestination 处理平衡括号，不 rstrip合法URL尾括号。仅完整独立 text token/行中的 BV/av 可复用现B站裸ID入口，不给任意数字补URL。Markdown label内URL不二次提取。未消费的文本出现明显破损链接语法时，整 block给malformed_markdown_block诊断/ambiguous occurrence，禁止regex回退拆成假valid；其他block继续。该保守检测覆盖已定义合成形态，不承诺识别一切破损Markdown。行内代码、image、HTML不作投递；含html_inline整block跳过并诊断，fence/code/html block不扫描。一般prose仍在完整原bytes，不推断同题/附言，不裁掉它。

feishu_post 从 content 或 zh_cn/en_us 的 rows/entries读取，a元素保留真实href/text，Evidence.json_path定位原结构元素；不把label+href拼成链接。只扫描text元素，不扫描md/code/image等元素，并保留局部诊断；坏row/entry不吞其他有效anchor。title/旁prose完整在原bytes但不自动投递。此接口输入是message.content的原UTF8，不宣称读取了原HTTP或已认证原raw_json；未来只读投影负责身份与来源真实性。

先检查HTTP(S)/host/userinfo/任何显式port与控制字符（包括percent编码控制字符），再调用原平台本地 identity/input validators。URL字面IP含冒号拒绝；未知host为pending_route。平台错误保留reason，未调用网络/connection/session/preview。Douyin旧parse_work_url并非严格全路径，先施现单work形态fullmatch再对照原native结果；主页/集合为needs_scope。B23/XHS/Douyin短链为needs_resolution，不虚构native；范围为needs_scope，需原adapter后续确认，未在候选中展开。

status可为valid/invalid/ambiguous/needs_resolution/needs_scope/pending_route。transport保留query/fragment与签名，不因native canonical相同丢token；仅裸BV/av通过原validator给合法canonical transport。完全相同transport仅duplicate_of首发生位置，不删occurrence、不分配任务。相同platform/native的先前位置记录related_to，安全参数不同不成为exact duplicate；不跨消息按内容hash合并。微博short/native alias、短长链接不会离线猜等价。

描述性label合法。label本身是URL/支持裸ID时，已证native不同为label_href_identity_conflict；若离线不能证明则label_href_identity_unverified。紧邻link_close后的text以数字开头产生numeric_tail_after_markdown_link歧义；尾数字原样保留，绝不拼进native ID。空格隔开的普通数字/评论不按此规则拒绝。

普通多URL内部mode=independent，不由数量/主题/prose默认同题。只有显式参数开启pure group eligibility；所有members为valid叶子、同平台、至少两个不同native且没有输入诊断才eligible。duplicate标记仍保留，不能凑足两个native。eligibility没有建立集合、确认授权、调用模型或强制综合；collection_succeeded恒false。显式参数真实性由调用方负责。

测试源码覆盖14项/LF/CRLF、末项无效、MD描述与冲突/尾数字、malformed其他block、重复与query关联、合法括号/BV、autolink/reference/code/HTML、host安全、short/scope/route、富文本anchor/prose/局部坏项、准入失败和无HTTP。编码worker仅AST/读回/hash，未pytest。真实采集、网络禁止性全链验证、Store事务/重启/消息幂等、旧position兼容、同题新manifest顺序、回执/产品UI均未接入或验收；继续沿主控分配共享写集和具体demo批准，不因此模块存在宣称R06/R07产品修复。

2026-10-08 主控读完整三文件后，Luna/max 单次独立合成回归 26 passed、0 failed/error/skipped，pytest 退出0。使用旧 CPython3.11.16 环境仅作测试工具，先核 markdown-it-py4.2.0／httpx0.28.1；未安装或修改该环境。唯一可丢弃根 `/private/tmp/kd-v3-r06-tests-20261008.uBEtZu`，完整命令及输出见 `/tmp/kd-v3-r06-tests-20261008.md`，JUnit SHA256 `4e288ae93002f1be16554002d93d52d9f116f001fc95c2406de504a81772f972`。主控独立读回26节点及执行前后源／测试SHA；全部为合成候选，产品主链及上述未验证项仍未完成。

# 模型 JSON 与响应恢复

V1.3 K02 实现合同；源码级与合成回归不代表真实模型或最终双平台成品验收。产品依据为 BUG-20260914-04：只兼容完整外壳，保持原有业务验证及请求归属。

`v1/model_json.py` 先按 Python 原有 JSON 策略解析裸响应；失败后只接受 trim 后完整、唯一一层、小写 `json` 的三反引号外壳，开闭行独立，支持 LF/CRLF。不会扫描大括号、递归去壳、修正文、补字段或清理普通 Markdown。JSON 字符串内合法反引号保持原文；重复键及非有限数值的既有解析行为没有变化。

返回值包含 `value/envelope/parser_version/raw_sha256`；解析错误携 `envelope_invalid` 或 `json_syntax_invalid`。业务错误保留原顶层 domain code，可附 `schema_invalid/evidence_invalid` 分类。外壳通过不表示证据成立或结果已保存。

## 当前消费边界

| 入口 | 后续验证与恢复 |
|---|---|
| knowledge_model / knowledge_presentation | 原知识结构、来源证据编号/范围与展示规则；字段恢复仅允许 requested fields，绑定完整父响应 hash |
| 根 review_validation 当前与 retry resolutions | 原来源修复、位置、证据、未决疑点及逐项恢复限制 |
| reviewer（由其 Owner 串行接入） | 现场、response-chain resolutions、候选建议；review_responses 保存可证原响应，原适配器仍验证/合并 |
| ocr_review_policy | 原决策索引、来源位置、可靠性和核心影响；原始响应链保留，未新增无身份 OCR 缓存复用 |
| collection_model | 原 item/point/原证据关联校验；不把集合综合写成 SourceFact |
| settings.activate_codex | 原精确连通性对象检查 |
| structured_calls | 原 JSON Schema 与业务消费者；只有该组织入口保留原精确 schema 回显兼容 |

HTTP 外壳、数据库、配置、签名清单、OCR 引擎协议和普通 Markdown 未接此解析器。`faithful_review._parse_candidate` 是旧解析辅助函数；当前适配器调用 `review_validation.validate_response`。旧 knowledge_derivation/topic_indexing/growth_modeling 的历史解析器未全局改写；当前组织入口经 StructuredCalls 验证后向既有领域层传递其已解析对象。

## 请求回执

`ResponseReceipts` 在拥有者原生命周期目录内保存独立请求文件和显式 current/pending 指针。先 reserve request ID，再请求；拿到响应后先原子写原文与 receipt，再原子发布 pending。没有响应不生成 receipt；原文保存或指针发布失败会抛出，不能声称可恢复。

归属绑定 operation、source version/hash、请求契约摘要、无秘密的模型配置摘要、parent hash 与 requested fields。请求 ID 和原文 hash 逐一核对；旧孤立文件、断点孤立文件、来源/契约变化或篡改不按时间/文件名挑选。旧响应不能抢占较新的 current 请求，同请求另一份不同响应不能覆盖第一份。

重试重新执行当前 validator。`received/parse_failed/validation_failed/prepared` 区分解析/业务准备阶段；本模块没有数据库提交权，绝不标 `committed`。知识 prepared 在原数据库保存成功前继续可复用，由既有 item 生命周期在提交后清理；数据库唯一性/事务仍由 Store 负责。

相同响应与当前 parser/validator 的失败指纹不反复重解析，后续走原有新请求/字段恢复策略。知识展示失败保留父候选，只恢复原已证明的字段；字段响应保存中断可重解析其明确指针。原组织 accepted/展示恢复缓存条件保留；新 received 响应在结构验证前中断可重解析，未被业务接受的 prepared 响应不自动升级为组织成功。

知识同来源调用及 reviewer 外层响应链用已有跨平台进程锁，竞争调用返回可重试的 checkpoint 错误。短回执写锁及请求代际另外防止迟到覆盖。没有增加新任务队列、数据库 schema 或历史材料清理。

## 验证与限制

合成门入口：test_model_json、test_response_receipts、test_review_response_receipts、test_json_entrypoints、test_knowledge_presentation；相邻门继续 knowledge_model、review_resume、integrated_review_contract、organization_contracts、ocr_review_policy、codex/settings 与 collections。反例包括非法围栏、错证据/字段、旧孤立响应、source/contract/model/parent/fields 改变、原文 hash 篡改、双调用、迟到返回、保存中断、同失败有界重试、prepared 不冒充 committed。

真实兼容模型、正式 OpenAI/Codex 路径和 Windows 原生运行尚需独立受控环境证据；假客户端只证明消费端机制。没有读取正式知识数据、私人账号/浏览器凭据或发外部消息。本模块不以默认配置或旧报告宣称这些真实门通过。

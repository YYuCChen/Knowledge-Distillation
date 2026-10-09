# R06 receipt snapshot wire：隔离适配器与 QA

`multi_link_receipt.py` 只读取调用方提供的 receipt Mapping，不连接 DB、app/settings/credentials、网络或模型。`prepare_receipt_snapshot(receipt, new_receipt=False, wire_utf8=None, wire_sha256=None, part_positions=(), has_capture=False, bot_open_id=None)` 返回冻结 ReceiptSnapshot：route=frozen/legacy/rejected、固定reason、原receipt UTF8和可选纯候选/wire。frozen仅是输入与解析结果冻结候选，不是已写表、接收任务、来源采集或raw/wiki成功。

文件常量 WIRE_VERSION=1、R06_CONTRACT=r06-multi-link-candidates-v1、MARKDOWN_VERSION=4.2.0、SNAPSHOT_KIND=receipt_snapshot_v1 是明确wire合同，不读取__file__、源码hash或运行环境凭据。构建/source SHA仅QA登记；项目依赖锁与主控构建校验负责实际MarkdownIt4.2.0，不把常量作为运行环境已验证的证明。新候选调用既有冻结R06 parser；重放不调用parser、不修wire、不升级版本。

事件receipt从event.message.content解析JSON.text，history从body.content解析JSON.text；input_path包含显式$json跳转。text保持原mention/CRLF/Unicode完整UTF8，不读取剥mention后的receipt.text。无img/media的post保留整个message.content原JSON字符串UTF8，并让parser从真实anchor.href/text和JSON path取证；含图/媒体post拒绝anchor-only升级，留原产品route。未知locale/坏结构明确拒绝，不猜其他字段为正文。

app/message须与原raw_json内message_id和存在时header.app_id核对；namespace仍('feishu',app_id,message_id)。source.version=receipt_snapshot_v1:<原receipt raw_json UTF8 SHA>，仅标接收快照，既不是平台native编辑版本，也不是正式raw/source编号。raw_json本身是产品已持久化的规范JSON文本，不冒称原HTTP wire。真实性、绑定用户/chat与接收时间范围由外层已认证receipt保证；本纯模块不进行认证。

same_topic仅读取receipt的显式元数据。true必须有text消息的原mentions中已绑定bot的真实open_id/key且key在完整原text出现；bot_open_id由认证绑定投影提供。无法核对则固定reason拒绝；普通文字“同题”/主题相似或URL数量不推断。post同题metadata当前未有原产品等价bot证据入口，保守拒绝true而不从label/@文本猜测。mention证据冻结进wire，重放须用相同绑定事实核对。

wire冻结完整input_base64、原input SHA、原receipt SHA、input_path、namespace/snapshot kind/version、intent、固定parser合同、完整occurrences/diagnostics/mode/eligibility；position_count与每项position必须连续0..N-1，invalid/ambiguous不压缩。duplicate_of/related_to必须吻合已冻结transport/native的先前发生集合。json重复key/nonfinite拒绝。post anchor evidence进一步核JSON path实际href/text；Markdown仅核block行范围/token值内部一致，**不再解析验证token语义**。

重放必须提供持久化层可信的wire_sha256，先核wire byte digest，再从当前原receipt重取raw/path/inputbytes/namespace/version/intent，与完整wire source核对，不仅信digest；再核position覆盖、part_positions是范围内唯一整数、evidence、状态/原因/组合同。篡改digest、来源path/字节/身份、缺项/重复/错position、未知wire/parser版本均拒绝。没有原wire时，旧receipt（默认new_receipt=False）或任何已有part/capture全legacy，不parse、不迁移原位置。只有外层同receive事务确认的新且未绑定receipt才能传new_receipt=True；布尔值不能自行证明新收件。已有wire+capture冲突拒绝。

完整性边界：SHA不是签名，也不是DB CAS。若攻击者可同时替换可信wire digest、清单计数/全部结果及持久化身份，adapter无旧锚点不能证明原candidate集合；不以“重算hash并接受”作为修复。伴随表需原receipt与wire/digest原子冻结、不可变触发器/唯一PK及事务旧值核对，由Lagrange A1/schema唯一owner实施。本模块检查内部覆盖与来源绑定，不声称抵抗有权篡改整库者或重新证明原Markdown语义。

异常和wire理由仅固定码，parser详细platform_locator_invalid:*、input_admission_failed:*归一，不泄露任意异常str/源正文到错误reason。完整原receipt/source/wire仍是受控素材载荷，含私人字节，不是日志；不以reason去保存完整原件。invalid、pending_route、needs_resolution、needs_scope与pureeligible保留各自语义，不给暂态补failed/task ID，不对eligible创建collection。

最小DDL建议（只建议，未执行）：由A1在主迁移版本统筹`feishu_link_inputs(app_id,message_id,contract_version,wire_json,wire_sha256,created_at)`，PK/FK为原receipt app/message，wire_json UTF8 bytes持久化时须精确可回取（可用BLOB）；禁止UPDATE/DELETE冻结字段，insert与首次receipt同事务。进度仍复用原parts/preview，schema24登记与其余R01迁移由唯一owner决定；此模块没有DDL、initialize或产品接线。

合成测试源码覆盖事件/history原text bytes/path、mentions、post anchor/raw JSON、image route拒绝、旧part/capture不升级、重放不parser、invalid/重复不压位置、source/hash/path/覆盖/未知wire版本篡改、真实anchor证据重核、pending区别、固定异常reason。worker仅AST/静态读回/diff/hash，不pytest。主控验收R06冻结parser26pass与8915dac不是本adapter运行证据。产品handler/intake/Store/schema/scope/status/cards/UI、持久wire事务与真实数据皆未改、未验。

## 2026-10-08 独立定向回归

主控已读完整321行业务、217行测试和本文，并核对 Sol/medium 自身 JSONL；Luna/max 在 fresh 0700 根 `/private/tmp/kd-v3-r06-adapter-tests-20261008.SgCA1e`、env-i、CPython3.11.16 测试工具下仅运行一次 `tests/v1/test_multi_link_receipt.py`。29 passed，0 failed/errors/skipped，pytest exit 0。JUnit SHA256 `eb7fbaccef689180cbe1f630fccf2c13830d480f111e30951ccb99db91095fa3`，主控独立解析29节点并核业务/测试前后SHA不变：`b259d829f3c3ae515d27aa23a59d9f40bb7a44eb63e33839bd08fe346ab2f6b5` / `767deeb305fd58f499eba96556f161d21547d2c0ac7051f621e014989edd67c8`。

外层 zsh 包装在测试结束后误用只读变量 `status`，实际外层退出1；报告明确区分 pytest退出0与包装失败，未重跑。此处只验证合成receipt/wire，不初始化DB、不访问网络/模型/凭据/真实资料，不证明产品入口、持久冻结或采集成功。报告 `/tmp/kd-v3-r06-adapter-tests-20261008.md` 保留原日志、JUnit和退出记录。

# R05 静态网页来源 reader（内部候选）

2026-10-08，Asia/Taipei。只新增 `v1/web_article.py` 与合成测试；未接入 intake、router、pipeline、UI、Store 或 schema。调用 `read_web_article(url)` 返回 `WebArticle(capture, parsed)`。它不是 `CapturedMaterial`，不分配 `source_key`、稳定 ID 或正式来源身份；后续主控决定 raw 入库与接线合同。

同日主控验收补记：已读初稿与实际重复资格审计／失败合同差异，真实固定库的完整受影响回归为 69 passed、无失败／错误／跳过，退出 0。证据 `/tmp/kd-v3-r05-repetition-tests-20261008.md`，可丢弃目录 `/private/tmp/kd-v3-r05-repetition-tests-20261008.1svpvO`，JUnit SHA256 `00ef0de774997fa62de82dba807804b3a108792d0f5de0e811d9145be3c947ae`；主控另核对实际 JUnit 与源码／测试 SHA。最早缺库 skip 和后续重复正文丢失 FAILED 均保留。通过的是内部静态 reader 及检测后拒绝不完整正文的合同，不能称全部网页完整、动态采集、产品接线或冻结包验收。所需打包 metadata／数据／许可证仍待补齐。

## 当前能力与内容边界

- `capture` 在内存保留 submitted URL、final URL、每跳 URL/status/Location/实际连接 IP、原始 HTML entity 字节、content-encoding 解码前 wire body、charset 与依据。HTTP transfer framing 不属于 HTML 字节。不会写文件、Vault 或数据库。上层持久化之前这些只是内存候选。
- 正文和标题／作者／日期来自 Trafilatura 2.3.1 的 `extract_with_metadata`；关闭评论和公开 `deduplicate` 选项，开启表格、图片、链接和格式。用同版 `xmltotxt(document.body, include_formatting=True)` 生成 Markdown 正文，另保留抽取正文 XML 字节。采集层不主动摘要、重排或去重，不另造正文抽取器。2.3.1 内部仍有不受公开选项控制的重复清理；本 reader 对检测到的重复丢失拒绝候选，详见修复节。其他抽取启发式仍可能遗漏内容，不能保证任意网站无漏字。
- canonical 仅来自 HTML link 声明，保存声明及未验证的 provenance；不抓取 canonical、图片、图表资源或正文引用。metadata 同时保留 Trafilatura 的 extracted URL，不把其 fallback URL 自动标成 canonical。多 canonical 不擅自裁决唯一 URL。
- `ParsedSource` 复用现有 snapshot/metadata/lineage 形状；lineage 中的 hash 是完整性摘要，不是正式来源编号。不伪造 HTML 字节到正文字符的精确映射。XML 字节仍是内部对象，不声明可直接 JSON 持久化。
- coverage 区分 HTTP body 完整与文章语义完整性未验证；记录原 HTML 与抽取树的表格／图片数量。表格数量减少发出需复核标记；不能仅凭数量相同证明内容完整，也不能因丢了表格就把文章标成“无知识”。图表只保留引用，不执行 OCR 或下载。

## HTTP 安全及预算

默认固定 httpx 0.28.1、httpcore 1.0.9，运行时拒绝版本漂移。缺失依赖返回固定 failure。初轮未改锁／安装依赖；2026-10-08 后续独立依赖准备已更新 pyproject、uv.lock 和两份平台锁，详见下节。尚未安装正式应用或接线。

每跳先验证 scheme、credentials、标准端口和 hostname，再解析 DNS，检查全部结果均为允许的公网地址。拒绝 localhost、私网、回环、link-local、组播、保留地址、mapped IPv6、常见 IPv6 转换／隧道地址和保守的特殊用途段；混合 DNS 直接失败。选择已验证结果中的第一个 IP，不自动重试其他地址。

自定义 httpcore network backend 使用 AF_INET/AF_INET6 socket 的数字 sockaddr 连接，没有 `create_connection` 或二次 hostname DNS。连接后核对 peer address。HTTP Host、httpcore 原始 origin、TLS server_hostname 保持原 hostname，生产 TLS 使用系统默认校验上下文，不能由网页关闭证书校验。pool 无 proxy、无 retry、无 HTTP/2。每跳新建 httpx client，`trust_env=False`、关闭自动 redirect，不携带 cookie、auth、netrc 或 browser profile，Set-Cookie 不回传。

默认最多 5 次 redirect，30 秒总预算，所有跳累计 wire body ≤4 MiB、解压 HTML ≤8 MiB。DNS、连接、TLS、读、写和抽取共享 deadline，慢滴流不能重置时钟。DNS/抽取使用 daemon worker 等待截止时间；调用方超时返回，但 Python 无法强杀尚未返回的 libc DNS／抽取线程，长期服务集成时主控需评估并发／取消策略。没有新增进程或浏览器权限。

请求 `Accept-Encoding: identity`；服务器仍返回 gzip/deflate 时，以 zlib `max_length` 限制解码分配，拒绝截断、尾随字节及 concatenated gzip members。其他 content-encoding 明确不支持，不调用 httpx 的无界自动解压。超限／中断的 hop 只保留 hop 元信息及空 body，不把部分体冒充完整 raw；此前已完整取得的最终响应，在后续 charset／抽取／访问墙失败时保留到 `WebReadError.capture`。reader 不留存每跳 redirect body，只留跳转证据与最终取得的 body。

## 编码、访问墙与动态限制

HTTP 和头部前 1024 字节 HTML charset 声明必须一致。支持 UTF-8/BOM、ASCII、ISO-8859-1、CP1252、GB18030、GBK、Big5、Shift-JIS；解码始终 strict。无声明只尝试严格 UTF-8，明确标记 fallback；失败是 charset_missing。拒绝未知编码、冲突、替换字符和 NUL；不会通过 replacement 或猜测 legacy 编码静默造正文。错误但可合法解码的单字节 charset 不一定可检测，不保证自动识别所有 mojibake。

HTTP 401/402/403/429、密码字段、明确 login/paywall/challenge 容器及阅读墙文本、JSON-LD `isAccessibleForFree=false` 均保守失败，不绕登录、付费或反爬。这些是可审阅访问信号，不是对任意墙的完美分类；普通文章内引用墙文本或嵌入登录表单也可能保守拒绝。JS 要求／meta refresh 返回 render_required；其他无可抽取正文返回 body_missing。没有动态渲染、账号、浏览器调用或 fallback 外部云服务。专用 platform 的优先级仍由外层 router 负责，本 reader 不决定平台路由。

固定错误码：

| 范围 | 错误码 |
|---|---|
| URL/网络安全 | web_invalid_url、web_credentials_forbidden、web_port_forbidden、web_address_forbidden、web_dns_failed、web_transport_mismatch |
| HTTP/预算 | web_timeout、web_network_error、web_http_error、web_redirect_limit、web_redirect_invalid、web_response_invalid、web_response_too_large |
| 格式/编码 | web_content_type_unsupported、web_encoding_unsupported、web_compression_invalid、web_charset_missing、web_charset_invalid、web_charset_conflict、web_charset_unsupported |
| 访问/动态/正文 | web_login_required、web_paywall、web_access_blocked、web_render_required、web_body_missing、web_extraction_failed |
| 重复正文资格 | web_repetition_loss、web_repetition_unverified |
| runtime | web_dependency_missing、web_dependency_version |

全部继承现有 `SourceReadError`；不会返回 processed_no_knowledge。异常消息只含固定码，来源文本不作为公开错误消息；上层不可无筛查公开 chained exception 或 capture。

## 官方 API 核对

agent-reach 的 GitHub/gh 后端只读以下固定 tag 官方源码；没有读取真实 profile 或真实来源数据。

- [Trafilatura core 2.3.1](https://github.com/adbar/trafilatura/blob/v2.3.1/trafilatura/core.py)：`extract_with_metadata` 返回带 `.body` 的 Document，参数 include_comments/include_tables/include_images/include_links/include_formatting/deduplicate 与 date_extraction_params。
- [Trafilatura XML 2.3.1](https://github.com/adbar/trafilatura/blob/v2.3.1/trafilatura/xml.py)：`xmltotxt` 保留 Markdown 表格和 graphic 图片引用；格式化前复制正文树。
- [httpx transport 0.28.1](https://github.com/encode/httpx/blob/0.28.1/httpx/_transports/default.py)、[httpcore backend 1.0.9](https://github.com/encode/httpcore/blob/1.0.9/httpcore/_backends/base.py)：自定义 transport/request/stream 和 network backend 方法签名。采用公开 ConnectionPool/Request/Response 接口，未替换 httpx 私有 `_pool`。

## 验收交接给 Luna/max

测试 socket 为合成 HTTP 字节，仍走真实 httpx/httpcore 解析；真实 DNS、TLS 与 TCP 都被夹具隔离，不以此声称互联网/TLS live 验收。DNS 变更、redirect SSRF、Host/SNI、cookie/env proxy、响应及解压超限、访问墙、坏 charset、正文缺失、恶意正文惰性和超时有独立断言。注入抽取器明确标为 injected-test-extractor，不能当作真实 Trafilatura 证据。

独立的 `test_real_trafilatura_static_table_images_comments_and_repetition` 要求已存在 Trafilatura 2.3.1；没有时显式 skip。此用例验证真实库的表格、图片引用、重复语句、去评论、元数据和正文指令保留，但网络依然是合成夹具。skip 意味着真实抽取未验证，不能宣称 R05 已整体验收。

由 Luna 使用已核验 CPython 3.11.16 环境执行（下面 `TEST_PYTHON` 必须由主控指定真实已有路径，不安装依赖）：

```bash
cd /Users/chen./Documents/知识蒸馏器/V3.0开发/source
R05_TEST_ROOT=$(mktemp -d /tmp/kd-v3-r05-test-20261008.XXXXXX)
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src "$TEST_PYTHON" -m pytest -p no:cacheprovider \
  tests/v1/test_web_article.py --basetemp "$R05_TEST_ROOT" \
  --junitxml "$R05_TEST_ROOT/results.xml" -ra
```

初轮 Sol AST 静态自检通过；随后 Luna 报告 R05 63 passed、1 skipped，skip 为真实 Trafilatura 缺依赖。主控已读实际源码并暂接受静态内部合同；这不是产品／动态路径完成。新环境实际真实库用例随后 FAILED：两段各4次的原文只剩4次，不能沿用 skip 结论。原失败证据保留；当前新增拒绝门和回归仍待 Luna 复验，见修复节。本任务没有 commit/push/release、正式应用安装、正式数据接触或 UI 差异。

## 后续依赖锁准备（2026-10-08）

`pyproject.toml` 精确加入 `trafilatura==2.3.1`、`httpcore==1.0.9`，主依赖和 secondary 的 httpx 均固定 `0.28.1`。uv 0.12.16 在隔离副本中先对全部既有 registry 包设置精确约束，再只放开官方 METADATA 要求升级的两项；移除临时约束后再次锁定，实际 source `uv lock --check` 通过。不使用 `--upgrade`，没有改变不相关版本。未定位到专门锁生成脚本，没有新增或修改脚本。

| 新增包 | 版本 | 发行包声明的许可 |
|---|---|---|
| trafilatura | 2.3.1 | Apache-2.0 |
| courlan | 1.4.0 | Apache-2.0 |
| htmldate | 1.11.0 | Apache-2.0 |
| justext | 3.0.2 | BSD-2-Clause |
| dateparser | 1.4.3 | BSD-3-Clause |
| babel | 2.18.0 | BSD-3-Clause，另有 Unicode 数据许可 |
| lxml-html-clean | 0.4.5 | BSD-3-Clause |
| tld | 0.13.2 | MPL-1.1 OR GPL-2.0-only OR LGPL-2.1-or-later |
| tzlocal | 5.4.4 | MIT |

[官方 Trafilatura 2.3.1 发行元数据](https://pypi.org/pypi/trafilatura/2.3.1/json) 要求 `charset-normalizer>=3.5.2`、`urllib3>=2.8.0`，因此原锁 3.5.1／2.7.0 分别升级为最低满足版本 3.5.2／2.8.0（两者 MIT）。lxml 仍为 6.1.3，仅 uv 图新增 `html-clean` extra 边。两平台锁各新增 9 项，原有其余 Mac 148 项／Windows 169 项版本及直接 Git revision 不变，无删除。Windows 仅解析依赖闭包，未作原生安装／执行验收。

PyPI wheel `trafilatura-2.3.1-py3-none-any.whl` SHA-256：`f86bad2ee36f82884e14dee84c83aae0f0ac0127da5c65764e82f3846716b822`，与官方 JSON 一致；其源码和实际安装的两个 API 签名均匹配 reader 参数。wheel 内 readability fork 自身声明 Apache-2.0。已检查新包的 METADATA 和随包许可文件；[tld 官方发行声明](https://pypi.org/project/tld/0.13.2/) 给出多许可选项，不在这里擅自裁决选用哪种。发行工需保留对应版权、许可及数据声明，并核对所选发行方式。

新的诊断解释器：`/private/tmp/kd-v3-r05-deps-20261008.1wLhVZ/venv/bin/python`。基础运行时由工程 python-runtime.json 的 Mac 官方归档解出，归档 SHA-256 已核验 `768f05cf200273bbdda9a5955a5a6892a4b22f2a0b1e4b0a9160f5c7fce86816`，实测 CPython 3.11.16。现有 uv-managed 同版本可执行文件匹配归档，但其 libpython 不匹配，原因未查明；未修改它，也没有使用受保护旧工程 venv。

诊断 venv 只安装 R05＋pytest 闭包 28 包，版本均取最终锁；`uv pip check` 通过。这不是完整应用构建环境，没有安装 Docling／Torch／模型，也没有运行模型服务。实际包清单、锁差异、官方 wheel 源码及许可检查记录保留在同一临时根；主控/Luna 仍需要它，暂不清理。

Luna 的下一次单项真实库验收命令（Sol 未执行）：

```bash
cd /Users/chen./Documents/知识蒸馏器/V3.0开发/source
R05_REAL_ROOT=$(mktemp -d /private/tmp/kd-v3-r05-real-20261008.XXXXXX)
env -i PATH=/usr/bin:/bin TZ=Asia/Taipei TMPDIR="$R05_REAL_ROOT" \
  PYTHONDONTWRITEBYTECODE=1 PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 PYTHONPATH="$PWD/src" \
  /private/tmp/kd-v3-r05-deps-20261008.1wLhVZ/venv/bin/python -m pytest -q -ra \
  -p no:cacheprovider --basetemp="$R05_REAL_ROOT/pytest" \
  --junitxml="$R05_REAL_ROOT/results.xml" \
  tests/v1/test_web_article.py::test_real_trafilatura_static_table_images_comments_and_repetition
```

仅定位、未修改 `packaging/KnowledgeDistiller.spec` 和 `KnowledgeDistillerWindows.spec`：目前没有明确收集 trafilatura 的 distribution metadata、模块和资源（如 jusText stoplists、Babel locale data、tld 公共后缀资源）。reader 运行时用 distribution version 门禁，后续冻结包必须实际核验这些内容及许可证，不能以新锁或诊断 venv 成功证明成品可用。接线和必要打包改动仍由主控分配独立写集。

## 固定重复清理的拒绝门（2026-10-08）

Luna 真实库受控测试报告 `/tmp/kd-v3-r05-real-tests-20261008.md`，JUnit `/private/tmp/kd-v3-r05-real-tests-20261008.OsjlCr/results.xml`，SHA256 `3380336c5c9709c9f05f4fa689757b0e31f42d7314fc19c09bb42232ca93defd`：1 failed／0 skipped。表格、图片、评论和导航断言先通过，失败落在正文重复8→4，原证据未删除或改写。

官方 PyPI 2.3.1 wheel 的 `main_extractor.py` 与安装文件逐字节一致，SHA256 `08371cfa8748062900cc54bc985491be81efc614cef18069ab54ad98a498c620`。`extract_content` 第742–750行无条件删除与上一个输出元素相同、长度>50的元素，不检查 options.dedup。`recover_wild_text` 的集合／子串去重及 baseline 的 `dedupe=True` 是其他独立路径；`deduplicate=False` 仅关闭 LRU/文档级相关分支。受控 trace 显示 `_extract` 的正文和 temp_text 均8次，返回 `extract_content` 时正文4次、temp_text仍8次；因此不能把中间 raw_text 或保存的 raw HTML 当作 snapshot 完整的证明。fast／precision／recall 三个公开选项诊断均仍4次，没有找到公开关闭参数。

选择用户授权的保守路径：不改依赖版本、不 patch 库全局变量、不自造抽取器、不按相似文本拼回段落。对原始 HTML 的 DOM 做**资格核对**，正文仍仅来自 Trafilatura：

- 先保存原 DOM 的段落路径；在独立树中复用同版官方 RAW_TREE_PRUNE_XPATH／REMOVE_COMMENTS_AND_LISTS_XPATH，并排除语义 nav/aside/footer/script/style/template。该树只用于核对，不作为正文输出。
- 唯一外层 article 或唯一 itemprop=articleBody 是明确范围；重复组按段落原文的 NFC／空白归一比较计数，行内文本直接拼接，不凭标题／含义猜对应。记录原 DOM 路径、文本 hash、来源次数和抽取 p 次数；这些不是正式稳定 ID，也不是 raw 字节偏移。
- 明确范围内重复组已有同文抽取 p，但次数少于来源，返回 `web_repetition_loss`。无明确范围、整组不见或结构变化导致无法可靠核对时，缺口返回 `web_repetition_unverified`；不自动把疑似重复认定成来源错误。
- 异常保留完整 `capture.raw_html`／跳转链，以及 `coverage.article_completeness=rejected`、`source_qualified=false`、`next_step=review-original-html` 和重复核对结果。HTTP 已完整取得与来源正文被拒绝分别记录。没有成功 ParsedSource，没有 processed_no_knowledge；原 HTML 是待确认资料，不能当作已通过的正文。
- 通过只证明此范围内已核对的重复 p 次数。其他布局／div/list/quote/code、结构合并、CSS 隐藏的响应式副本、评论识别误差、Unicode 格式差异仍可能导致保守拒绝或未覆盖风险；整体文章完整性继续 unverified，不宣称 complete。没有重复组的非明确范围也标 `unverified-source-scope`。

原用例仍保留两段各4次的 fixture，成功分支仍断言8，不改成4；新拒绝分支必须断言固定失败、原件8次、来源两段／抽取一段、明确范围、原件待确认及不 qualified。这是已授权的失败闭环，不是把少了一段的候选当作成功。独立真实库回归另验证表格/图片/元数据/去导航评论、段内重复20次、短相邻重复2次、长非相邻重复8次，以及明确 articleBody／歧义范围的拒绝。

Sol 只在已准备3.11.16诊断 venv 做最窄离线语义诊断：原失败素材当前被拒绝；短相邻2、长非相邻8和段内20保留，表格/图片仍在、噪音不在。结果保存于临时根 `repetition-diagnosis.json`／`repetition-after-diagnosis.json`，不等于 pytest 通过。没有执行 pytest；Luna 下一次使用上述隔离命令，将末尾节点替换为 `tests/v1/test_web_article.py`，执行完整受影响 R05 文件并保留新 JUnit。依赖四文件保持本轮修复前 SHA256 不变。

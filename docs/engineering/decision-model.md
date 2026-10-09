# R17：内部文本 SystemOne 客户端与私有 profiles

状态：2026-10-08 第一批实现候选。仅新增客户端、profile 服务及合成测试；未接 settings、UI、captures、recall、schema、app 或功能 flag，未部署／启动 Clef，也未访问正式数据或权重。测试执行和最终验收分别交 Luna/max、主控。

2026-10-08 主控内部验收补记：已实际审阅客户端、profile、并发修复及测试。首次并发失败保留；定向修复后独立 Luna/max 回归为 133 passed、无失败／错误／跳过，pytest 与包装命令均退出 0。证据 `/tmp/kd-v3-r17-fix-tests-20261008.md`，可丢弃目录 `/private/tmp/kd-v3-r17-fix-tests-20261008.zvyJ6h`，JUnit SHA256 `0cd117d0e35f35b934707ac780d0e8fc95a2bc8bc95a6b52575fbcbce990f911`。主控另读实际 JUnit、核对源码与测试 SHA；源码初次内部提交为 `2afc96bd77c3db88a8c97f0aa8abd6922e0cddfe`。该结论只覆盖合成内部合同，不代表真实 Jev／Clef、设置交互、业务接线或发布已验收。

## 调用合同

`DecisionProfile(provider, endpoint, model, auth_ref, protocol, timeout_seconds, token_budget)` 为不可变配置。provider 仅 `jev`／`clef`，协议固定 `systemone-text-v1`。当前预算限制为 1–16384，默认 16384，超时 0–600 秒（不含 0）。

`DecisionClient(profile, secret=resolver, post=fake).ask(state, questions)` 接受 string／JSON object／array 和 `dict[str, ChoiceQuestion | NoulQuestion]`，一次请求返回 `DecisionResult`。instructions 支持 string／object／array，choice criteria 为 2–255 个完整命名选项，可用 null 描述；noul criteria 可省略或包含 true／false。只支持文本，不发送顶层 images／videos，不做自动分块、摘要或阈值判断。

响应必须包含 model、全部且仅全部问题 ID、匹配的 type、usage 的非负整数 input_tokens／output_tokens。choice 必须含所有且仅所有选项，概率／confidence 为有限非 bool 数值且在 [0,1]，所选概率必须是最大值（同概率允许任一最大选项）。不填补缺项，不转字符串，不重新归一化。Jev 分布和的容差 1e-6；Clef 因四位小数舍入采用 `选项数 × 0.00005 + 1e-9` 的误差上限。

结果保留 provider、protocol、requested_model、实际响应 model、usage、原概率及 confidence。Jev 标记 `jev-normalized-concentration`，保留服务值，不重算；Clef 标记 `clef-max-probability`，验证 confidence 等于最大概率。noul 仅 P(yes)，不存在 confidence；收到 noul confidence 保守拒绝。多问题不是统计独立性声明。现有身份／召回阈值迁移与中文校准不属于本批。

Jev 当前仅接受官方 HTTPS 完整 endpoint，auth_ref 必填，并由 resolver 注入非空密钥。Clef 仅接受字面量 `http://127.0.0.1[:port]/v1/systemone`（可配置端口），拒绝 localhost、其他回环／LAN、userinfo、query、fragment、相对或不完整路径。Clef auth_ref 可空；指定引用时仍必须能解析非空密钥。认证不写进 profile，回调 repr 隐藏，不记录正文／凭据／远端错误体。默认 HTTP 客户端、注入 POST 和 GET 均显式禁用 redirect 和环境代理；注入调用须接受 `follow_redirects=False, trust_env=False`。任何 redirect 都拒绝，无重试、云回退或进程控制。

稳定错误包括 decision_profile_invalid、decision_request_invalid、decision_secret_unavailable、decision_timeout、decision_request_failed、decision_redirect_refused、decision_unauthorized、decision_busy、decision_response_invalid、decision_model_mismatch、decision_budget_exceeded、decision_probe_transport_required。413 同时覆盖服务 body／模板上下文预算，不误报成素材无价值。

## 完整模板预算与服务前置条件

Clef 每请求带 `truncate:false`。未来授权部署必须固定已审 revision，并使用 `--host 127.0.0.1 --max-length 16384 --no-truncate --quiet`；如调低 profile.token_budget，服务 max-length 也必须相应匹配。max-length 是 state、所有问题／选项、系统前缀、schema 字段、assistant 后缀的**总模板预算**，不能将 16384 全给 state。profile 不能让客户端截断输入；本客户端没有启动或配置服务的权限。端口冲突由用户处理，不杀进程。

`clef_template_upper_bound` 逐段复现该 revision 的文本模板渲染，累计每段 UTF-8 字节作为 byte-BPE／`add_special_tokens=False` 条件下的保守上界，结果明确 `exact=False` 和方法名；可能提前拒绝实际能容纳的输入。部署需核验实际 tokenizer 确实符合该条件。未下载 tokenizer、不执行上游代码、没有真实 tokenizer 计数证据，不能称预检为精确 token。精确判定由服务 tokenizer 与 no-truncate 的 413 完成；成功 usage 超过 profile 预算亦拒绝。

Jev 预检仅为 request UTF-8 字节的保守准入限制，不包含未知云端模板，不能证明云端总模板 token 上界；记录不同方法名。当前两类预检都不代表模型实测。未来若需要更充分利用上下文，先验证 tokenizer／准确计数合同再替换估计，不以静默截断提高通过率。

公开脚本全文审阅结论：load 在目录不存在时会 snapshot_download；因此部署必须先只读核验明确现有模型目录，不能直接传未核验 repo ID。head 严格加载、MLX 联合评分；HTTP handler 允许 truncate 请求覆盖，并在线程锁内串行推理；schema 超限和 total 超限均 413。服务自身不认证，允许自定 host／name／max-length，且图像路径会抓取远端 URL；本客户端只发文本，部署仍须单独核对绑定、日志与启动参数。

特别限制：该 revision 的 systemone 响应 model 回显请求值，不能用这个 echo 核验加载模型身份。`verify_local_model()` 必须显式注入 GET transport，请求同 endpoint 的 `/health`，核验 status=ok 和 served_name 与 profile.model 精确相同；否则 decision_model_mismatch，缺 GET 则 decision_probe_transport_required。不会创建默认 GET 或自动调用未授权本地服务。GET 禁 redirect／环境代理，错误保持 safe codes。profile 验证必须先过此 GET，再执行 choice+noul；仅 health 通过不授予激活资格。

served_name 是服务报告的模型身份，不是权重证明，且 GET 与推理之间服务可被其他人替换；不能证明权重 revision、joint head 真实性、服务 max-length 或绑定地址。这些由后续受权部署的文件清单、参数与本机合成推理证据补齐，不能以 HTTP200 或模型名回显宣布真实服务验收。

## 私有持久服务

`DecisionProfiles(root, secret=resolver, post=fake, get=fake_get)` 必须显式给绝对独立目录；只创建末级目录（0700），父级由调用方准备。拒绝所有 symlink 路径组件、公共 root、非本用户所属 root；macOS `/tmp` 是 alias 时请显式使用 `/private/tmp`。状态和锁文件 0600，拒绝 symlink／hardlink／非普通文件／其他所有者／宽松权限，不自行 chmod 用户旧目录。

`add_draft(profile)` 生成新稳定 ID，不改 active；draft 不原地修改。`get(id)`、`active()` 只读回合同。`validate_draft(id)` 先清掉该 draft 旧验证资格，Clef 用显式注入 GET 核验 served_name（Jev 不访问未经官方定义的 health），再执行同一合成 state 上 choice+noul 的真实客户端合同检查，期待 choice=match、noul>0.5，成功只写验证记录及 served_model。`activate(id)` 必须显式调用并要求已有合成成功记录。失败旧 active 原样保留，成功不自动 activate；active 不可重验证，需新 draft。验证只证明当时合同，不承诺长期服务存活或未来密钥有效。

持久状态采用单份版本化 JSON；严格 key／profile／active 引用／验证结构检查，包括重复键和 NaN。损坏文件不会重置为默认。私有目录 fd、O_NOFOLLOW、线程锁＋flock 序列化读写和完整探测；临时文件 fsync 后原子 replace，并 fsync 目录。替换前写入失败保留旧文件，清理仅本次临时文件。磁盘在 replace 后 fsync 失败属于提交持久性不确定，调用方需读回，不能误称旧文件必然未变。不可防止拥有本用户权限的进程主动修改文件；该目录不是签名认证数据库。当前实现限 macOS/POSIX，未声称 Windows 支持。

## 验收入口与来源

Luna 仅执行 `tests/v1/test_decision_client.py`、`tests/v1/test_decision_profiles.py`，env-i、TZ=Asia/Taipei、显式独立 basetemp。测试全部 fake HTTP／合成 profile，不使用正式密钥、DB、Vault、browser profile、模型服务。交接文件 `/tmp/kd-v3-r17-client-handoff-20261008.md` 给出命令、基线和实际静态检查。本文及 handoff 中的测试命令是待执行，不是已通过声明。

- [TypeSafe HTTP API](https://docs.typesafe.ai/api)、[confidence](https://docs.typesafe.ai/confidence)、[choice](https://docs.typesafe.ai/primitives/choice)、[noul](https://docs.typesafe.ai/primitives/noul)、[state](https://docs.typesafe.ai/concepts/state)：2026-10-08 可读普通页面。
- [Clef 完整已审脚本](https://huggingface.co/mlx-community/clef-4bit/raw/5c646a43ed30b5d79822b16e11c6b3d621800b94/clef_mlx.py)：只读 curl 全文，不执行、不触及权重。
- [根 R17](../../../docs/requirements.md#r17-决策模型云端本地可配复用-clef-mlx-服务已定案)、[根架构](../../../docs/architecture.md)、[testing](testing.md)。

## 首测并发缺陷与修复候选（待 Luna 重跑）

首测 client 95 passed，profile 31 passed／1 failed（并发多实例创建 draft）。证据 `/tmp/kd-v3-r17-r05-tests-20261008.md`；冻结首测 JUnit SHA256 `5209da47b2b666bfe8429072f523d50adc92e65a35f2c62eea7d993ef8158263`。R05 不在本次重跑范围。

必要语义诊断使用 CPython3.11.16、env-i／TZ=Asia/Taipei、新建 `/private/tmp/kd-r17-errno-diagnostic-t7e9kcde`，12 次原并发操作有 2 次失败；读取 __context__ 得到底层 FileNotFoundError／errno=2，栈定位 `_locked` 的 os.open（不是 flock）。只输出异常类型、errno、函数／行号和合成根，不回显配置内容。

进一步最窄合成对照 `/private/tmp/kd-r17-openat-errno-bp_d5kl0`：6 组×4线程并发首次开锁失败14次，预创建锁失败0次；失败 open 的 directory fd 仍是目录，随后的不跟随stat显示锁为普通文件／nlink=1。由此定位本机首次并发 `openat(O_CREAT|O_NOFOLLOW|O_NONBLOCK)` 竞争路径，未猜测内核机制、未归罪于 flock。保留O_NONBLOCK用于避免异常FIFO阻塞；不删除或重建活锁。

修复改为先不带O_CREAT安全打开既存锁；ENOENT时用O_CREAT|O_EXCL独占创建，竞争者收到EEXIST后重新安全打开，最多3次；保持目录fd、O_NOFOLLOW、0600、所有权／regular／nlink检查和原跨进程flock。获得锁后再次验证fd与锁名dev/ino一致且锁名不是symlink，替换锁立即拒绝，避免不同锁inode的并行写入。独立算法诊断 `/private/tmp/kd-r17-exclusive-open-diagnostic-egic_m54` 的6组×4次均获取成功；这是定位证据，不能替代源码pytest验收。

补充回归设计：屏障同步的首次／既存锁并发多实例；确定性注入竞争创建EEXIST并断言NOFOLLOW／EXCL未弱化；等待锁期间被替换为symlink或新普通文件均拒绝且不写状态；三个独立合成子进程共同写24个draft，不丢更新。网络侧以真实httpx.post helper＋MockTransport和合成HTTP_PROXY／HTTPS_PROXY验证trust_env=false传到其Client，redirect=false传到request，整个测试无真实网络。原Jev alias→resolved model、原生概率语义和20秒默认保持；未来settings候选120秒应显式配置。

本轮只改原五文件。未运行pytest、未触及真实服务／资料／其他人文件；由主控审新字节后交Luna仅重跑两decision测试。

## 2026-10-08 SettingsService延迟后台桥接（未接产品入口）

主控裁决：本批仅mac/POSIX私有profile配置；Windows新profile方法返回`decision_profiles_unsupported`，不切旧Jev、不实现Windows锁。凭据沿现LocalSecrets合同：Mac是0700目录、0600文件中的明文受权限保护（非加密），Windows是当前用户DPAPI；profile/Store/日志/响应只存引用，不存或回显API Key。没有改Keychain策略、迁旧key或读取实际凭据。本期JSON无明文限制不指现专用凭据文件。

SettingsService构造新增`decision_root/decision_profiles_factory/decision_secret_factory/decision_post/decision_get`注入，仅记录参数；不读profile、凭据或新目录，不check、不启服务。明确后台操作才延迟创建DecisionProfiles；默认根为`store.path.parent / 'decision-profiles'`，沿其安全目录/锁/原子写入检查，不追随别名回退。构建检查仍不实例化SettingsService或调用state/secret方法；原有service构造平台/组件行为未变，不能声称整个旧Settings构造是纯对象。

- `save_decision_draft(profile_fields, api_key=None)`：真实字段为DecisionProfile的`auth_ref`，只保存新immutable draft；API Key编辑总是保存到新`decision-key-<uuidhex>`账户（现凭据save，pending_validation），不覆盖旧ref/active，不自动GC失败留下的孤立key。缺key/非法引用/非法profile固定失败；无网络检查，返回draft ID。传入引用限定此账户格式，加载仍由后端检查真实存在/权限；不会自动复用jev-api-key。
- `check_decision_draft(draft_id)`：唯一显式配置检查，经原validate_draft先失效旧资格，再合成choice+noul；Clef先served model GET。post/get均需显式注入，缺失注入拒绝transport，绝不默认发检查网络。失败资格清空、旧active保留；成功返回draft/provider/requested model和固定合成contract，不回显原服务response。配置资格是检查时的事实，不是永久健康承诺。
- `activate_decision_profile(draft_id, expected_active_id=...)`：先读不可变draft descriptor，明确内部activate在锁内CAS要求当前active等于expected，再检查draft资格并写入；成功返回committed activation snapshot（提交时配置快照），不是永久当前active。另一合法activate可随后改变当前状态，不能据锁外读回把已成功提交误报conflict；当前状态另由decision_state读取。replace/fsync持久性错误照实报错，不能承诺旧active未变。无consumer registry、用户bool或featureflag。产品没有新activate route，不能称业务已切换。
- `decision_state(draft_id=None)`：只读非敏感active descriptor及可选draft资格，不check/加载key。该组合展示不是消费提交CAS；提交必须用当前版本/expected_active重新核验。
- `decision_client()`：仅当前active返回`(profile_id, DecisionClient)`，客户端构造不加载key/ask；无active返回None，不调旧jev_client。损坏/不安全配置返回固定错误，不伪装无配置；后续consumer无新active必须pending。secret resolver延迟加载且异常固定`decision_secret_unavailable`，provider失败不换本地/云/历史Jev。

DecisionProfiles新增`qualification(id)`只读checked/active（不返回validation原响应）；`activate(id, expected_active_id=...)`在同一原文件锁内CAS。旧内部`activate(id)`省略参数仍兼容原测试，无新增格式版本。draft不可编辑，修改model/endpoint/预算/超时/key均新ID未检查；原active及旧检查记录保持其原版本，不复制到新draft。

旧jev_state/jev_client/save_jev_key、原settings view、LLM/ASR/平台、app/Feishu/Captures/wiki、模板/routes/static全部未接线。旧已有消费者继续原Jev语义，新typed client不适配成旧choose/raw ask；Clef confidence不能冒充Jev或照搬身份0.9/0.8阈值。R16/wiki typed消费者及真实版本/CAS接线经验收后，才由主控授U06具体demo/启用route及发布；本批内部activate合成成功不等于产品启用。

新增合成test_settings_decision_profiles.py：内存凭据、store facade（无DB）、显式可丢弃根、fake HTTP；构造零新增IO、草稿与key编辑失效、显式检查/启用、重启active、坏配置不fallback、固定secret错误、旧Jev兼容、缺transport检查失效、provider失败及Windows拒绝。test_decision_profiles.py追加资格只读与多实例并发CAS一胜一冲突。编码阶段仅AST/diff/static QA，未运行pytest、未读取真实profile/key、未调用网络/模型；验收由主控/Luna一次运行冻结文件。

后台桥接限定回归已执行一次：Luna/max、CPython3.11.16/pytest8.4.2、fresh159工具环境、env-i与独立0700根 `/private/tmp/kd-v3-r17-settings-tests-20261008.xgUQrA`，两文件67 passed（profiles39/settings28），无failure/error/skip，pytest/外层exit0。JUnit SHA `d86ce49174b926673479e4edb58463e370302d652b4b8dd35c85504f23c4df00`；五文件及Store/database依赖测试前后SHA无漂移。主控实际读代码diff、完整新测试及JUnit67节点，核固定提交回执/并发CAS。全部fakeHTTP/memorysecret/StoreFacade，未初始化DB或读取真实配置、密钥、Vault、模型、网络；不证明U06/R16/wiki接线、Windows持久层、真实模型或完整发行通过。

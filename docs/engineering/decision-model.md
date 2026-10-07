# R17：内部文本 SystemOne 客户端与私有 profiles

状态：2026-10-08 第一批实现候选。仅新增客户端、profile 服务及合成测试；未接 settings、UI、captures、recall、schema、app 或功能 flag，未部署／启动 Clef，也未访问正式数据或权重。测试执行和最终验收分别交 Luna/max、主控。

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

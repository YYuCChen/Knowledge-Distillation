# 双平台候选构建

此入口只创建候选，不发布、不安装，也不重启机器。Mac 与 Windows 使用同一完整 Git 提交及显式版本号。每轮从提交生成独立干净源码，源码 bundle 上传后核对 SHA256。

本机配置（Python、只读模型缓存、SSH 密钥路径、已固定主机指纹文件、Mac签名配置）放仓库外 JSON。不要提交机器配置或密钥。使用 `scripts/dual_build.py --config <配置> --version YYYY.MM.DD.N`：

- `prepare`：锁定当前提交和版本，创建两端独立源码与请求。
- `start`：启动两端持久 worker，重复执行复用运行中/成功任务。
- `status`：读取两端真实状态；`failed`、`interrupted`、`cancelled` 或 `invalid-artifacts` 均不是候选成功。
- `start --retry`：显式重试失败平台，新增 attempt，保留旧日志；成功平台经摘要验证后复用。
- `cancel`：请求取消自有构建子进程，不涉及日常应用。
- `collect`：取回成功平台ZIP和报告，并重新校验大小及SHA256；只有两端均成功才显示 both_candidates_built。

构建状态、每步日志、失败原因与产物摘要保存在作业目录。Windows worker 显式脱离 SSH Job，并用自身 Job 管理构建子进程；Mac 构建子进程继承宿主锁，因此主控被强杀时不会与重试并发。宿主重启后丢失的 worker 显示 interrupted，必须显式重试，不伪造成功。

原生桌面凭据验收使用临时合成数据，在用户已登录的 Windows 桌面进行；公钥 SSH 无法解密用户 DPAPI 不是跳过测试的理由。候选启动与归档检查不代表正式数据升级、Windows 11/ARM64、首次账户授权或公开发行已通过。

## 发布与验证边界

此脚本只生成候选。正式发布或替换安装包需要单独授权，并分别记录两端的真实验收结果。候选构建成功不代表公开发行已完成，也不证明历史发布包与当前源码逐字节一致。

配置示例见 `dual-build-config.example.json`。复制到仓库外，填写实际工具、缓存、签名配置和经核实的主机信息；不要把私钥内容或私人配置放入源码。

## 受宿主监督的构建作业

托管 CI 可能禁止 Windows `CREATE_BREAKAWAY_FROM_JOB`。这类环境可显式使用
`scripts/build_job.py submit <job> --request <request.json> --supervised`，让 worker
保留在宿主作业内；宿主结束时它可能一同结束，不能宣称能跨 SSH/CI 退出持续运行。
默认双主机构建仍要求独立后台 worker，权限不足会明确失败，不静默降级。

# 质量入口、影响计划与证据护照

V1.3 的 K00 入口为 `scripts/quality.py`，只编排登记的 pytest 和现有构建/候选工具。使用仓库 `python-runtime.json` 指定的实际 Python；命令参数是数组，护照和本地输入里的 shell 文本不会被执行。`.yaml` 登记文件使用 JSON 兼容子集，入口没有新增 YAML 或测试框架依赖。

```sh
python scripts/quality.py plan --base <commit> --head HEAD --output <new-plan-directory>
python scripts/quality.py run --plan <plan-directory>/plan.json --gate module --data-root <new-disposable-directory>
python scripts/quality.py status --plan <plan-directory>/plan.json --gate module
python scripts/quality.py report --plan <plan-directory>/plan.json --gate native --output <report-file.json>
```

计划必须在已提交且干净的实际 HEAD 上生成；输出必须在源码树之外。base=head 仍运行质量入口自验。退出码 0 表示所请求门的要求全部有有效通过，1 表示已知失败、失效或阻断，2 表示配置/环境/runner/证据缺口。报告在失败时也输出逐项状态。门按 module→contract→candidate→native→release 累加。未映射新增和删除路径阻断所有门，开放 S0/S1 阻断 candidate 及以上门。缺少原生 runner 会在计划中公开列出，阻止相应 native/release 门；不妨碍独立模块工作。skip、空测试集合、退出码 0 但业务结果不成功均不算通过。

`quality/change-impact.yaml` 精确列出现有产品文件及明确新增文件；新增产品文件须补归属。依赖边传播到消费者，场景护照同时绑定其上游依赖。测试/文档/质量采集器修改使相关证据失效，但不触发未变产品重建。新增测试还必须接入对应场景 runner，单纯归属登记不等于已经运行。初始 synthetic 家族只是既有回归；其通过不宣称完成冻结矩阵的新场景。真实模型、浏览器、飞书、远端和原生场景分开列出，尚未提供 runner 的项目保留明确缺口，不能通过人工改等级来晋级。

运行需要全新独立数据根；只允许同一计划再次使用已标记根。正式应用、Application Support/AppData、Vault、恢复工程、发行候选等保护位置拒绝使用，符号链接先解析。子进程获得独立 HOME、AppData、TEMP、pytest basetemp 和应用数据变量，避免默认路径进入正式数据。登记的产品 runner 仍须显式传 `--data-dir`；这些环境设置不是针对恶意测试代码的操作系统沙箱。计划和数据根各有操作系统排他锁；中断不会留下可冒用的成功，原始日志及每轮护照保留。工具不自动删除任何测试根。

护照保留原始源码 commit、实际平台/Python、场景和 runner/fixture/组件摘要、起止时间、命令、退出码、结果、stdout/stderr/JUnit 哈希及未证明范围。工具校验护照自身摘要与所有输出内容；摘要用于发现损坏/失效，不是对恶意重写整个仓库及证据的密码学签名。仓库 runner 登记是受审代码，不能从外部护照获得执行权限。

可选 `--release-input <local.json>` 只接受这些字段：

```json
{
  "version": "<build-version>",
  "product_version": "1.3",
  "build_job": "<existing-local-job-directory>",
  "build_config": "<private-dual-build-config-file>",
  "parameter_lock": "<measured-parameter-lock-file>",
  "artifacts": {
    "macos-arm64": {
      "path": "<candidate-archive>",
      "sha256": "<actual-archive-sha256>",
      "build": "<candidate-build-directory>",
      "build_sha256": "<quality.tree_digest-of-build-directory>"
    }
  },
  "reuse_evidence": [
    {
      "passport": "<old-plan>/evidence/<scenario>.json",
      "plan_dir": "<old-plan>",
      "reason": "具体说明依赖、配置与平台未变的兼容理由"
    }
  ]
}
```

本地输入和实际日志不默认适合公开，私有配置内容不会复制进计划；配置身份以摘要绑定。旧证据复用核对组件及上游、runner、fixture、场景契约、平台、实际 Python、成品和日志哈希，复制原护照但不改它的 commit，在新计划登记原 evidence_id 和理由。远端可用性不可跨计划复用。新成品需要新完整包及构建树身份；旧模型组件自身证据不能代替新主包组合旅程。

`build_job` 和 `dual_build` 适配器调用它们已有的 `status` 接口，等待中的任务保持 not_run，不把后台提交成功当构建通过。本地 job 检查实际产物哈希与源码；dual status 只证明两个任务状态，不证明远端资产字节或产品旅程。构建的 submit/run/prepare/start/collect/cancel 继续由这些已有工具负责，质量入口不另造构建执行器，也不自动触发昂贵构建。

`verify_candidate` 适配器沿用其实际 `--platform --build --output --version --commit` 参数及独立数据输出，只登记原工具覆盖的冻结运行链、helper、启动、页面和版本身份。Dock、真实模型、安装器、迁移回退、线上读回必须各自有专用 runner；不能因为 frozen smoke 通过就填写它们通过。

事故索引包含需求/冻结依据、原现场索引、合成回归、预期断言、级别/平台场景、Owner、状态和处理决定。初始 unknown 表示尚需按实际结果核定，不能据此消除已知严重问题。延期记录必须有明确下一轮目标和已验证安全绕行；S0/S1 只能 must_fix_now。规格的原件保留在项目治理执行核验入口，公共索引不复制私人现场。

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

## V1.3 最终接线

通用 pytest 家族中平台专属断言按完整 nodeid 拆分登记：Windows worker 子进程清理、NTFS/COM/长路径，以及 Mac 上的 POSIX 删除边界在 contract/integration 门独立运行。原通用场景的 `deselect` 指向对应独立场景，不能用于取消必需检查；平台错误或所需 HDiffPatch 未提供仍为 not_run。

浏览器适配器直接执行受注册文件摘要约束的已有源码 runner，不导入外部 JSON 作为通过结果：

- `desktop_browser` 编排现有 fixture 和 protocol/slow-open 两个 Chromium runner，保留参数锁、原始子进程日志和资源截图，仅证明源码桌面协议。
- `manual_browser` 执行 `tests/v1/browser/manual_browser.py --output <独立目录>`，重新核对 production home.js 摘要、实际平台/Python、全部浏览器命令退出状态与播放/暂停各 20 条原始样本；缺记录、时间超限、输入/音频身份或摘要异常不通过。只覆盖 reconciler 子项，不能替代完整 M06、原生 IME、真实组提交或飞书设备。

这些场景登记为 integration；没有把合成 pytest 或源码浏览器晋级成 native/visual 成品。原生 Dock、真实模型、真实飞书客户端仍缺专用 runner，继续列为 not_run。

成品 `verify_candidate` 适配器除退出码外核对报告中的实际平台、源码/版本、独立数据声明、冻结主程序与 helper 运行时、两次启动和页面数量；保留 runtime、helper、启动原始日志和报告。所有成品场景仍绑定当前 archive 与 build tree 摘要。对旧护照复用也重新解析必需输出；改写护照 passed 并重算摘要不能覆盖 JUnit 或业务报告中的失败。质量采集器摘要参与证据依赖，改采集器只失效证据，不触发产品重建。

开放 S0/S1 允许采集登记场景以取得修复证据，但 `status/report` 与门退出码仍为 blocked，并列出逐项状态。未映射路径继续禁止执行。事故索引中的模块交付日志是可核对线索，不能当作当前候选护照；安装接受回退 S0、正常零页恢复 S1 在成品门未关闭。

当计划源码晚于成品时，成品源码身份来自受 build tree 摘要绑定的 `build-manifest.json.git_head`。它必须是计划 HEAD 的祖先。差异仅限测试、文档或精确登记的质量采集器时沿用原规则；若存在独立组件改动，则按 `artifact_inputs.main` 和历史/当前两份依赖图取主包输入闭包，逐 Git blob 与文件模式比较旧新输入，未知路径、闭包内容变化或依赖遗漏均拒绝。不能将整个 installer 目录豁免。护照同时保留计划源码、`artifact_source_commit` 和 `artifact_input_proof`（实际原来源、祖先关系、输入摘要及独立变化路径）；主包产品、打包或依赖输入有变化即拒绝。此规则不重写成品原始 commit，也不把采集器变更当作需要重新构建产品。

音频探针登记为 contract/integration。可选本地 `audio_input` 格式为：

```json
{
  "fixtures": "<independent licensed speech directory>",
  "permission": "self_created",
  "fixture_hashes": {
    "source.m4a": "<file sha256>", "standard.wav": "<file sha256>",
    "short.wav": "<file sha256>", "script.txt": "<file sha256>"
  },
  "components": {
    "macos-arm64": {
      "root": "<independent measured ASR component tree>",
      "tree_sha256": "<quality.tree_digest>",
      "python": "<actual component Python within root>",
      "model": "<model directory within root>"
    }
  }
}
```

把上述对象作为 release-input 的 `audio_input` 值；Windows 同理增加 windows-x64。许可支持 self_created/redistributable，仍须保留许可出处与真实词语脚本。PCM 探针重算标准化和五个片段字节，真实引擎探针核对组件树、Python/worker、各调用输入/原始结果/日志与分段连续性。缺平台组件、夹具或执行失败保持未验/失败；不会自动下载模型。该证据不证明识别语义无误、用户原音故障或最终主包旅程，native 音频门仍独立存在。


## 组件来源与双平台汇总

`installer` 只拥有 bootstrap 入口及它的直接测试；`installer_shared` 拥有主程序/helper 也固化的安装模块，以及由 `application_datas` 复制的安装器 HTML/SVG。主程序与独立安装器均依赖 shared。`installer_platform` 经两平台 spec 的 hidden import 和 helper 的快捷方式调用进入实物，不能因只改 picker 函数就视为 installer-only。独占 bootstrap 修改可只重建独立安装器，前提是对目标成品导入清单的核验与登记闭包一致；新增文件和动态依赖须补映射。

独立 installer 的版本和源码由自己的 build-manifest 记录。`verify_candidate` 仍只验证主程序及包内 helper，`prepare_component_release` 仍要求 recipe 来源等于目标主程序实际来源。交付清单分别列主包、installer、模型的版本、源码和字节身份，不把 installer 新提交写入旧主包。

每次实际 runner 执行前使用同一解释器独立采集 `execution-environment.json`，绑定真实 system/machine、OS、Python、解释器路径。汇总验证的是执行环境而非汇总机 OS。旧护照没有独立环境记录时仍只能在原平台/同 OS 范围复用；不能通过改 platform 获得 Windows 证据。外平台场景在当前机器不执行，也不写入冒充本机执行的占位护照，报告保持 not_run。

Windows 执行完实际 contract 后，在同一干净源码和原实测组件仍存在时导出：

```sh
python scripts/quality.py export --plan <windows-plan>/plan.json --output <new-private-bundle>
```

导出重新核对原计划、输出哈希、音频组件/夹具和配置身份；保留 plan/passport 原字节，复制原始输出和许可合成音频夹具，组件树只重新测量身份，不复制模型。私有构建配置只记录摘要，不复制内容。bundle 仍含本地路径和诊断，须保留在本地交付范围，不自动公开。

将 bundle 取回 Mac 后可只读核验原 Windows 报告：

```sh
python scripts/quality.py report --plan <bundle>/plan.json --source-root <exact-clean-checkout> --gate contract --output <windows-report.json>
```

`--source-root` 必须指向原 plan 的同一源码提交、干净 checkout；注册、runner、fixture、上游源码和所有输出逐项重查。原 Windows `C:\...` 路径按 Windows 路径语义映射到 bundle 中同字节 PCM，不访问汇总机上伪造的 C 盘路径。该离线报告证明记录时的实际运行与组件身份，不声称远端机器此刻仍可用。

当前 Mac 计划和 Windows 计划采用相同源码提交及发行目标后，可集中汇总：

```sh
python scripts/quality.py report --plan <mac-plan>/plan.json --gate contract --peer-bundle <windows-bundle> --output <combined-report.json>
```

peer 只补精确匹配其执行平台的 source/synthetic/integration 子场景；保留原 evidence ID、peer plan ID 和各平台配置/音频身份。`host` 行不混用，native、model_real、visual、remote_readback 不通过 peer 导入放行。缺场景、原始失败、hash/契约不符、开放 S0/S1 仍然阻断。最终成品及独立 native/发行门继续使用各自实物证据。

## Docling 显式组件与文档工作流

`test_submitted_sources.py::test_document_uses_same_durable_worker_and_locator_without_audio[pdf]` 与 `[epub]` 从 `SC-MANUAL-FIFO--synthetic` 精确拆出，分别登记为两平台的 `SC-DOCUMENT-WORKER--pdf/epub--<platform>`，证据等级为 integration。仍运行原始节点和原断言，辅助生成材料的 `test_document_sources.py` 同时参与输入指纹；不会因拆分而漏掉去重、worker中断恢复、来源定位、发布及模型调用次数检查。

PDF 场景要求 release-input 显式提供对应执行平台的独立组件：

```json
{
  "docling_input": {
    "windows-x64": {
      "models_root": "<absolute-independent-model-directory>",
      "component_identity": "<canonical-trusted-manifest-identity>",
      "tree_sha256": "<quality.tree_digest-of-this-platform-model-directory>"
    }
  }
}
```

Mac 使用 `macos-arm64` 键。`models_root` 指向含 `manifest.json`、RapidOcr、Docling模型目录的那一层；必须是绝对路径，不能借系统或正式资料目录。身份来自源码受信清单的规范JSON摘要，工具逐项核对清单、文件内容、目录树及普通路径，拒绝链接/重解析点。路径只定位输入，不作为跨机组件身份。EPUB 仍执行真实Docling解析，但不要求PDF权重，独立登记为无模型依赖。

计划、执行前、结果核验及导出都重查组件身份。只向对应场景注入 `KNOWLEDGE_DISTILLER_DOCLING_MODELS`；HF缓存位于本次attempt内部，`HF_HUB_OFFLINE=1`、`TRANSFORMERS_OFFLINE=1`，不会依赖用户原HOME缓存或隐式联网补模型。每次执行生成 `execution-docling.json`，记录实际Docling、docling-core、docling-ibm-models、Torch、ONNXRuntime、RapidOCR版本、组件身份及模型路径/离线环境，并与JUnit及护照输出哈希绑定。缺显式组件/运行依赖保持not_run，字节或实际结果错误阻断。

复用须匹配 `docling_inputs`。portable导出保留受核验的组件身份与原运行记录，不复制1GB级模型；汇总端只核验原计划、源码、receipt及输出，在内存中使用原身份记录，不打开原机器绝对路径。`peer-bundle` 可汇总这些精确平台integration结果，不能将它们升级为最终冻结应用验收或声称原机器此刻仍有模型。

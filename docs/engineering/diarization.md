# R12 独立后台候选：文字对齐与本地 pyannote 适配器

2026-10-08，Asia/Taipei。主控已审阅最小 API proposal、实际源码及修复差异，独立 Luna/max 合成复验 64 passed，无失败、错误或跳过。本候选未接入 pipeline/ASR/Store，没有 UI；保留 Qwen。未安装依赖、加载模型、下载权重或处理真实音频。真实声学效果、部署及断网运行均未验证。

## 纯对齐合同

入口为 `knowledge_distiller.v1.diarization.align_transcript`，显式接收 recording_id、完整原 text、transcript_provenance、整录音 duration、TimedTextSpan、整录音 SpeakerInterval 与可选 ExistingLabel。数据类型均为 frozen dataclass；模块导入只使用标准库。没有写入、ASR 重跑、模型加载或语义整理。

- char 为 Python Unicode code point 半开区间，不是 UTF-8 byte/UTF-16 偏移。原文逐字保留，包括空格、标点、换行、emoji；输出覆盖全文，连续、无漏无重。空正文返回空分区。
- timing 为整录音秒数半开区间。word/boundary/chunk 必须附匹配 provenance；调用方声明的证据类型不等于程序验证了声学真实性。文本与 timing 的来源引用保持独立。
- 一个完整输入 span 的每个时间子区间必须由唯一同一个 speaker 连续覆盖，才归属匿名 speaker。小缺口、短插话、混合 overlap、跨人轮次都不能按最长占时归整段。
- 完整 span 全程同时有多人时标 overlap；单人/多人混合或多人接续缺少可信 char 时间切点时标 unknown，并给出 needs_local_alignment。没有时间的字符也保留 unknown，不继承相邻人。
- 每个输出分区保留原 TimedTextSpan 的 char/时间窗口及实际 granularity；标签边界可能拆分 char 分区，但不会拆分或插值窗口秒数。输出窗口不能当作每个字的精确时间。
- word/boundary 的重叠时间覆盖、倒序、char 重叠/越界、bool/NaN/Inf、非正时长、负时间及超音频时长拒绝，抛带 code/input_ref 的 DiarizationContractError；不隐式截断。粗 chunk 合法但重叠的时间窗口保留 unknown/coarse_time_coverage_conflict。
- 匿名 A/B/... 由整录音首现时间排序，同起点按原 speaker_key 确定次序；同人跨块不重新编号。不跨录音识别人，不保存声纹，不推断实名。
- `preserved_labels` 保留字幕/用户确认的原身份、范围、label 与 provenance，是来源归属的优先依据。`status/speaker_id` 单独描述匿名声学候选，不能替换原标签；不同命名空间不做“名称不一致”比较。同区互斥来源标签全部保留并报告 label_conflict，强制 status=unknown、speaker_id=None，声学 candidates/evidence 留存，不擅自选一个。此分区的 needs_local_alignment.resolution_kind 为 source_label_review，即需来源/用户归属复核；重跑声学不能解决标签身份冲突。其他时间缺口仍标 acoustic_alignment。

## 实际 ASR 数据缺口

`adapters/qwen_worker.py` 的 Mac 调用使用 return_chunks=True、return_timestamps=False、forced_aligner=None。Windows worker 依据 PCM/静音/长度分 chunk；`qwen_component.py` 返回 QwenRuntimeResult，`knowledge_distiller/primary.py` 转为仅含 text/start_seconds/end_seconds/language 的 PrimaryChunk。豆包 `_translate` 同样只保留 utterance 级窗口。

这些入口没有本合同所需的可信 word/boundary char 区间，也没有整录音 speaker 身份。不能将 chunk 等同发言轮次，不能重新拼 chunks 文本替换原 text。跨人 chunk 仍需另行授权的细时间对齐或边界局部识别提供可靠 char→时间证据；本模块只返回缺口，不执行这些动作。

## 官方版本、许可与永久链接

本轮通过 agent-reach 的 GitHub/gh 后端只读官方代码。Git tag 4.0.7 是 annotated tag `a85203407840885b6f8d8535299c694526191b6e`，解析到 commit `b749285c5cdd4636b2edc7f766f1352c8dde9369`。以下均为该 commit 的永久链接：

| 核验对象 | primary 证据与结论 |
|---|---|
| 代码许可 | [LICENSE](https://github.com/pyannote/pyannote-audio/blob/b749285c5cdd4636b2edc7f766f1352c8dde9369/LICENSE)：MIT；与模型许可分列 |
| Pipeline 加载 | [core/pipeline.py](https://github.com/pyannote/pyannote-audio/blob/b749285c5cdd4636b2edc7f766f1352c8dde9369/src/pyannote/audio/core/pipeline.py)：from_pretrained 支持本地目录、文件或 dict；不存在的路径会走 HF；本地 checkpoint 不接受 revision |
| 子模型解析 | 同文件 expand_subfolders 将 `$model/subfolder` 变为 checkpoint/subfolder 参数；dict 模式的 model_id 为 cwd。本候选不依赖 cwd，先将获支持的三个子模型显式绑定为绝对本地 root/subfolder |
| Model 加载 | [core/model.py](https://github.com/pyannote/pyannote-audio/blob/b749285c5cdd4636b2edc7f766f1352c8dde9369/src/pyannote/audio/core/model.py)：本地目录按 subfolder 加载 pytorch_model.bin；不存在的路径可走 HF；checkpoint 使用 weights_only=False 并解析模型 class，资产及运行代码必须受信任且独立隔离 |
| 模型文件名 | [utils/hf_hub.py](https://github.com/pyannote/pyannote-audio/blob/b749285c5cdd4636b2edc7f766f1352c8dde9369/src/pyannote/audio/utils/hf_hub.py)：Model=pytorch_model.bin、Pipeline=config.yaml |
| PLDA | [core/plda.py](https://github.com/pyannote/pyannote-audio/blob/b749285c5cdd4636b2edc7f766f1352c8dde9369/src/pyannote/audio/core/plda.py)：本地子目录加载 plda.npz 与 xvec_transform.npz |
| embedding | [speaker_verification.py](https://github.com/pyannote/pyannote-audio/blob/b749285c5cdd4636b2edc7f766f1352c8dde9369/src/pyannote/audio/pipelines/speaker_verification.py)、[getter.py](https://github.com/pyannote/pyannote-audio/blob/b749285c5cdd4636b2edc7f766f1352c8dde9369/src/pyannote/audio/pipelines/utils/getter.py)：mapping 转入 pyannote 的本地 Model loader |
| 重叠输出 | [speaker_diarization.py](https://github.com/pyannote/pyannote-audio/blob/b749285c5cdd4636b2edc7f766f1352c8dde9369/src/pyannote/audio/pipelines/speaker_diarization.py)：DiarizeOutput 有 speaker_diarization 与去重叠的 exclusive_speaker_diarization；本候选只读取前者，itertracks 原秒数，不用 serialize 的舍入时间 |
| telemetry | [telemetry/metrics.py](https://github.com/pyannote/pyannote-audio/blob/b749285c5cdd4636b2edc7f766f1352c8dde9369/src/pyannote/audio/telemetry/metrics.py)：PYANNOTE_METRICS_ENABLED=false 使 track 函数不创建指标 span；导入仍初始化 OTLP exporter/processor，禁指标不等于网络隔离 |
| runtime | [pyproject.toml](https://github.com/pyannote/pyannote-audio/blob/b749285c5cdd4636b2edc7f766f1352c8dde9369/pyproject.toml)：Python>=3.10、torch/torchaudio>=2.8.0、torchcodec>=0.7.0，另含 Lightning/OpenTelemetry/pyannote 等；这是上游下界，尚非已解析锁或本机可用环境 |

官方 [pipeline.py 配置执行入口](https://github.com/pyannote/pyannote-audio/blob/b749285c5cdd4636b2edc7f766f1352c8dde9369/src/pyannote/audio/core/pipeline.py#L259) 会把旧 version 改写为 dependencies、构造指定 pipeline 类、将顶层 params 传入 pipeline.instantiate；freeze/preprocessors/device 和 hparams_file 还有独立执行入口，preprocessors 可以构造任意类或读取 FileFinder 配置。本候选拒绝这些顶层额外字段，也不传 hparams_file。dependencies 交给 [check_dependencies](https://github.com/pyannote/pyannote-audio/blob/b749285c5cdd4636b2edc7f766f1352c8dde9369/src/pyannote/audio/utils/dependencies.py)，解析版本并查询本地 importlib.metadata，不在该函数中安装、读模型或联网。

本候选不再原样透传任意顶层 params/dependencies/version：params 仅允许已读 SpeakerDiarization 数值组 segmentation(min_duration_off/threshold)、clustering(threshold/Fa/Fb)，值必须有限 int/float，bool/字符串/路径/URL/未知键全部拒绝。dependencies 仅允许精确 `{'pyannote.audio': '4.0.7'}`；version 仅允许字符串 4.0.7 且不得同时提供 dependencies。gated config 若需要更广的本地包版本声明，先审阅并明确扩展合同，不能自动放宽。pipeline 构造参数另限已知 bool/正 batch integer/有限正 segmentation_step/固定 VBxClustering 与三个显式本地子模型。PYANNOTE_SKIP_DEPENDENCY_CHECK 绕过开关被拒绝。数值组来自同 commit 的 [SpeakerDiarization](https://github.com/pyannote/pyannote-audio/blob/b749285c5cdd4636b2edc7f766f1352c8dde9369/src/pyannote/audio/pipelines/speaker_diarization.py#L289)，不是已获取 gated 配置。

community-1 revision 固定为 `3533c8cf8e369892e6b79ff1bf80f7b0286a54ee`（主控传入），wheel SHA-256 为 `852ea15c4d85bc34773e618267603ffca6a521669a74d33742692cc67fc700d6`（主控传入，本任务未下载 wheel）。模型为 CC-BY-4.0，gated 条款要求用户亲自接受联系人共享等条件，本候选不代接受或索要 token。[官方模型页面](https://huggingface.co/pyannote/speaker-diarization-community-1)；[固定 revision 页面](https://huggingface.co/pyannote/speaker-diarization-community-1/tree/3533c8cf8e369892e6b79ff1bf80f7b0286a54ee)。未获取 gated config/权重内容，不能把代码入口核验当成该模型配置实测。

## prepared_local_candidate 与执行门槛

`PyannoteLocalAdapter.prepare()` 只核验显式绝对本地目录、非 symlink 资产路径、规范相对路径、调用方 manifest SHA-256、固定 revision/runtime 声明、许可接受证据引用，返回具体 missing/problems/verified_assets；不导入 pyannote，不联网，不写环境、不安装。每次检查五个必须条目：config.yaml 与 segmentation/pytorch_model.bin、embedding/pytorch_model.bin、plda/plda.npz、plda/xvec_transform.npz。缺 manifest 条目逐项返回 manifest:路径；列出但文件缺失则返回路径；哈希错及缺许可另报 problems。两项 fake assets 不再可能报核心 manifest 完整。

“prepared_local_candidate”始终是候选状态，deployment_verified=False。core_manifest_complete=True 只表示所声明五个必须条目及 manifest 已列资产在该次预检中均通过、许可/runtime 声明无问题，不证明全量传递依赖资产完整、真实模型内容、许可真实性或 runtime 可用；fake 字节即便哈希匹配也不是模型证明。prepare 不解析 gated YAML，不声称模型可运行。引用是审阅记录入口，不是机器能够自证的授权/隔离事实。

`diarize` 默认因缺 ExecutionPermit 拒绝。未来独立组件进程需外部确认 authorization_ref、network_isolation_ref、config_review_ref 及专属 runtime_python；native 还要求 asset_immutability_ref，引用外部确认的只读/不可变资产快照与所有权边界。引用非空仅是入口必填检查，不能证实实际网络或文件隔离。不能给当前应用解释器补几条环境变量就冒充隔离环境。真正执行依旧需主控获取用户授权并核验这些证据。

native lazy loader 在导入前检查解释器路径、已导入 pyannote、固定库版本、PYANNOTE_METRICS_ENABLED=false、HF_HUB_OFFLINE=1、HF_HUB_DISABLE_TELEMETRY=1。它不设置全局环境，不安装服务；环境变量只是上游开关，不能保证所有依赖无网络请求。外部网络隔离必须先成立，并另行实测。

YAML 只接受限制结构的 SpeakerDiarization；segmentation/embedding/plda 必须明确为 `$model/segmentation`、`$model/embedding`、`$model/plda`，拒绝未知类、远程/带 revision 子模型引用、隐式默认子模型及上述任意路径参数。之后转为绝对本地 checkpoint/subfolder，调用官方 `Pipeline.from_pretrained(dict, token=False)`。该受限形状依据官方 loader 设计，尚未与 gated config 核对；不兼容时明确失败，不修改 gate 或自动拉文件。

native 入口独立重核 root/链接/manifest 全部哈希、五个必须条目及许可/revision/runtime 声明；配置读取后校验 SHA，再解析同一份已核验 bytes，不二次打开配置。Pipeline 导入后、from_pretrained 前再全量预检，加载后再次检查，变更即拒绝继续音频推理。**这些复核仍不能消除 TOCTOU**：底层 loader 会再次按路径读权重，路径/目录在检查与反序列化之间被换掉仍可能已被加载；事后哈希失败无法撤销 weights_only=False 的执行。实际授权部署必须提供不可变/只读资产边界并验证，asset_immutability_ref 字符串不提供这个边界。候选没有复制/冻结/挂载/锁住资产，也没有加载权重验证，故继续保持部署未验。

调用前仍需确认全部依赖/模型资产许可、config 与 manifest 内容审阅、可信 checkpoint、完整文件哈希、隔离目录与网络策略；真实 pipeline 加载、标准输出类型、音频解码、模型效果及 telemetry 行为没有运行验证。fake loader 的成功不提升部署状态。

## 待补部署材料与成本

未盘点本机模型/依赖，不声称权重确定缺失。主控传入核心 metadata 合计 32,820,977 bytes，仅是模型 metadata 字节合计；不是完整环境体积、下载证据或峰值内存。

需后续授权后补：用户亲自接受 gated 条款的证据、完整 revision 资产清单与 license/attribution、获准的本地组件路径、gated config 兼容性、隔离 Python 3.11 契约及依赖锁/轮子 ABI、torch/torchaudio/torchcodec 的平台适配、网络隔离实测。应用 torch/Qwen/MLX 环境及项目锁完全不变。磁盘、RAM、峰值内存、耗时均无本机实测数字。

## 验证状态

已编写 meaningful fake tests：多人及非交替、短插话、完整/混合 overlap、细小缺口、跨块稳定匿名身份、粗 chunk 跨人、部分 word times、标点空白 Unicode、来源标签及冲突、完整 char 分区、异常时序/超界、资产哈希/缺文件/许可/runtime、symlink/远程拒绝、未授权拒绝、prepare 不加载、普通重叠输出和 config 本地约束。

独立复验报告 `/tmp/kd-v3-r12-tests-20261008.md`；数据根 `/private/tmp/kd-v3-r12-tests-20261008.ZWxn6o`，JUnit `results.xml` SHA256 `dbcad1eddcd72de3cebab0e6851993c149fec381713213dd8ca76e28cf8d79dd`。使用旧工作区既有 CPython 3.11.16 仅作测试工具，`env -i`、明确新 source PYTHONPATH、独立 basetemp、禁用第三方 pytest 插件和 cacheprovider；单次 `tests/v1/test_diarization.py` 退出 0，64 passed（0.10s）。主控独立解析实际 JUnit，并核对源码 SHA256 `b9a21ce1eb7c1c7afece6be03dab3181ad9dff1f9b42bedf99e691a908ac2044`、测试 SHA256 `38136278fae385c5930628886aba3673d34c9f70decdb7263bb97e5aaa96ea27` 与执行前后均一致。未重试或修改断言；全部输入和 loader 为合成，未触达模型、真实音频、网络、正式 DB 或 Vault。该结果只验合同与拒绝路径，不提升 deployment_verified。

下面是开发时留下的命令形状；实际单次执行目录、命令与退出以独立报告为准，不为包装验证重复运行。后续新改动如需测试，仍须独立可丢弃 basetemp 与受控测试 runtime，不安装模型依赖：

```sh
PYTHONPATH=src python -m pytest tests/v1/test_diarization.py --basetemp=/tmp/kd-r12-synthetic-tests-UNIQUE
```

UNIQUE 必须换成确认不存在的新目录，避免 pytest 清理既有内容。合成结果只证明对齐合同与 fake 调用约束，不能证明真实声学效果或部署安全。

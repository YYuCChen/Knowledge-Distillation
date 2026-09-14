# K04 音频边界探针

探针用独立测试目录和已安装测试组件执行；不使用正式知识数据，不下载模型，也不修改借用的组件目录。运行驱动与组件解释器均须为项目规定的 Python 3.11.16。

## 数据与证据分开

准备自创有词语音或许可清晰的语音：`standard.wav` 为至少 312 秒、单声道 16 kHz PCM16，`short.wav` 为同源前 24 秒。保留文字脚本、来源、语速、生成命令、原始音频和哈希。可用本地语音合成，但不能以纯音、随机波或零 PCM 代替实际 ASR。

另准备 `source.m4a`，用于验证压缩媒体经过产品标准化后与固定解码参考的 PCM 一致。对 AAC 的比较基准是解码 PCM，不能要求它等于有损编码前 PCM。

```sh
PYTHONPATH=src:. PYTHONDONTWRITEBYTECODE=1 /path/to/python311 \
  scripts/probe_audio_boundaries.py --engine mlx \
  --python /path/to/test-component/python/bin/python3 \
  --model /path/to/test-component/model \
  --worker "$PWD/src/knowledge_distiller/v1/adapters/qwen_worker.py" \
  --fixtures /independent/fixtures --output /independent/new-output
```

Windows 使用 `--engine transformers`、该测试组件的 `python/python.exe` 和 `qwen_windows_worker.py`。跨机传输源码时必须包含 worker 同目录的 `python-runtime.json`，不能只传 `.py`。

探针真实运行 300 秒主识别分段、8 秒独立定位窗、恢复子段，保留每次调用的输入 PCM 哈希、帧数、耗时、退出码、引擎原始结果和错误日志。恢复测试只在根节点注入两次 incomplete；子段仍调用真实引擎。此项证明恢复机制，不声称真实引擎自行发生了根节点失败。定位识别不能替换原来源文字。

```sh
PYTHONPATH=src:. /path/to/python311 scripts/probe_audio_pcm.py \
  /independent/fixtures /independent/new-pcm-output
```

PCM 探针实际调用产品标准化与裁片器，验证首尾以及跨 8、20、300 秒窗口逐帧一致。这里的目标标签/时间为受控输入，不能用来证明 ASR 提供了真实逐词时间。

`trace_qwen_pcm_reads.py` 可包裹 Windows worker，记录实际输入 `readframes` 的起始帧、帧数和内容哈希，不改变字节；用源 PCM 对应区间的哈希逐项核对。它只用于显式探针，不作为产品 worker 或打包输入。

## 判断边界

- EOS、段数、PCM 连续、缓存命中分别记录，不能彼此替代。
- 音频断裂与 ASR 文字差异分别记录。边界附近文字差异须做同 PCM 复跑和跨边界上下文对照；上下文对照正确不自动证明原分段切掉了音素。
- 若无法证实用户原问题，记录 `not_reproduced` 及实际覆盖，不泛称全部通过。
- 回听默认最多约 10 秒。已定位目标超过窗口或超出源音频时明确不可用，保留现有恢复入口；不能裁掉目标后标可用，也不能退回短候选绕过已知目标范围。
- 若真实证实分段丢词/重复，再为受影响层增补核心区归属、上下文去重、全局时间、缓存版本和额外推理成本的方案，之后实施最小修复。无证据不统一增加全部引擎重叠。

## Windows 分段边界修复

固定20秒切点的真实语音反例曾在140秒附近稳定丢失 `wooden bridge`；同PCM跨界识别可补回。Windows worker现在在20秒上限内优先选择持续至少60ms的低能量停顿，避免把短塞音闭塞当停顿。候选点须在核心起点后至少5秒，短尾段保持原帧区间。

核心区按整数帧连续划分，每帧只归属一个片段，不加入语音重叠，也不删除重复词。输出时间直接来自实际帧界。worker内容哈希属于既有缓存身份，因此Windows旧识别缓存不能误用于新算法，Mac身份保持不变。

没有满足条件停顿的强连续信号仍使用原有20秒有界切点；这不是所有语音边界识别准确性的保证。此fallback、短尾、短闭塞和整段帧归属均有机制测试，真实引擎还需对实际词句和新切点逐项复验。

Windows整段实跑可加 `--trace-worker-pcm`，同时核对 `decoder_piece_writes` 哈希、输出块实际起止帧和源PCM；不能只数模型输出块数。

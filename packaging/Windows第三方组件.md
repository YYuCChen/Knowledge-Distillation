# Windows 第三方组件与来源

核对日期：2026-09-07。下表说明本次构建使用的主要组件；完整 Python 版本清单见包内 `build-dependencies.json` 与 `windows-requirements-lock.txt`，模型文件、固定提交和 SHA256 见 `_internal/docling-models/manifest.json`、`_internal/paddle-models/manifest.json`。各第三方组件保留自身版权，具体条款以附带许可证及上游声明为准。

| 组件 | 本次版本与用途 | 来源与许可声明 |
| --- | --- | --- |
| Python | 3.12.10，基础运行时 | [Python](https://www.python.org/downloads/release/python-31210/)，PSF 及其附带第三方许可，见 `licenses/Python-LICENSE.txt`。 |
| Node.js | 24.19.0，OpenCLI JavaScript 运行时 | [对应版本完整 LICENSE](https://github.com/nodejs/node/blob/v24.19.0/LICENSE)，Node.js 自身采用 MIT，内置依赖各自条款见该文件；补充副本为 `licenses/models/Node-v24.19.0-LICENSE.txt`。 |
| FFmpeg / FFprobe | 9.0.1 essentials，音视频恢复与转换 | [Gyan Windows builds](https://www.gyan.dev/ffmpeg/builds/)。实际二进制声明 `--enable-gpl --enable-version3 --enable-static`，随构建 README 标注 **GPL v3**；见 `licenses/FFmpeg-LICENSE`、`licenses/FFmpeg-README.txt`。源码定位为上游提交 [bf1b838f2a](https://github.com/FFmpeg/FFmpeg/commit/bf1b838f2a)。 |
| PaddlePaddle / PaddleOCR / PaddleX | 3.3.0 / 3.7.0 / 3.7.2，Windows OCR | [PaddlePaddle](https://github.com/PaddlePaddle/Paddle)、[PaddleOCR](https://github.com/PaddlePaddle/PaddleOCR)、[PaddleX](https://github.com/PaddlePaddle/PaddleX)，对应已安装 wheel 声明 Apache-2.0；许可证在 `licenses` 对应发行名目录。 |
| Docling / Docling Core / Docling IBM Models | 2.126.0 / 2.95.0 / 4.0.2，文档解析运行代码 | [Docling](https://github.com/docling-project/docling)，MIT。代码许可与下面的模型权重许可分别记录；Docling 元包缺失的正文已补为 `licenses/models/Docling-v2.126.0-LICENSE.txt`。 |
| RapidOCR / ONNX Runtime | 3.9.2 / 1.29.0，Docling 扫描文档 OCR | [RapidOCR](https://github.com/RapidAI/RapidOCR/tree/v3.9.2)，Apache-2.0；[ONNX Runtime](https://github.com/microsoft/onnxruntime)，MIT 及第三方通知。正文与通知补在 `licenses/models/RapidOCR-v3.9.2-LICENSE.txt`、`ONNXRuntime-1.29.0-*`。 |
| OpenCLI | 1.8.7，平台来源读取 | npm `@jackwener/opencli`，Apache-2.0；本次 npm 包的 LICENSE 保留在 `_internal/opencli`，并补充到 `licenses/models/OpenCLI-1.8.7-LICENSE.txt`。其 `node_modules` 内的依赖分别保留自身声明。 |

## 随基础包提供的模型

| 模型仓库或模型组 | 许可声明 |
| --- | --- |
| [docling-layout-heron](https://huggingface.co/docling-project/docling-layout-heron) | Apache-2.0 |
| [docling-models](https://huggingface.co/docling-project/docling-models)，含表格模型 | CDLA-Permissive-2.0 |
| [CodeFormulaV2](https://huggingface.co/docling-project/CodeFormulaV2)，公式与代码模型 | CDLA-Permissive-2.0 |
| [PP-OCRv6_small_det](https://huggingface.co/PaddlePaddle/PP-OCRv6_small_det)、[PP-OCRv6_small_rec](https://huggingface.co/PaddlePaddle/PP-OCRv6_small_rec) | Apache-2.0 |
| RapidOCR 使用的 `PP-OCRv6_det_small.onnx`、`PP-OCRv6_rec_small.onnx`、`ch_ppocr_mobile_v2.0_cls_mobile.onnx` | 下载自 [RapidAI/RapidOCR 的 v3.9.2 模型版本](https://www.modelscope.cn/models/RapidAI/RapidOCR/files?Revision=v3.9.2)，该版本模型卡声明 Apache License 2.0；模型卡副本随包保存。 |

许可证正文、仓库修订与补充文件下载来源位于 `licenses/models` 的 `models.json`、`supplemental-notices.json`、`Apache-2.0.txt` 与 `CDLA-Permissive-2.0.txt`。补充文件的 SHA256 单独记录，未修改模型权重。

## Qwen 选装边界

基础包不含 Qwen 专属 Python 环境或 Qwen 权重。用户在设置页明确下载后，安装独立运行时和 [Qwen3-ASR-1.7B-hf](https://huggingface.co/Qwen/Qwen3-ASR-1.7B-hf)；该模型卡声明 Apache-2.0。独立环境中的 Python、PyTorch、Transformers 等各自许可证随其下载内容保留。安装完成与启用分开；具体运行能力见测试说明。

## 分发材料核对范围

本次已补齐发现的 Node、Docling、RapidOCR 与 ONNX Runtime 许可文件遗漏。FFmpeg README 提供 FFmpeg 主仓库源码提交，但本次尚未逐项核对该静态构建全部外部库的对应源码及构建脚本，也未把仅有上游链接认定为满足所有再分发义务。扩大对外分发前，应按实际分发方式核对对应源码材料；[FFmpeg 官方许可说明](https://ffmpeg.org/legal.html)列出了相关要求。本文记录已核查的技术材料，不对整包给出法律合规结论。

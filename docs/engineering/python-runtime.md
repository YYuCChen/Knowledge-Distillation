# V1.2 Python 运行环境契约

用户裁决：Mac 主程序、Windows 主程序和全部 Qwen Python 运行环境统一为 Python 3.11。本轮固定同一补丁版本 **CPython 3.11.16**；产品版本继续 1.2。

契约在 `src/knowledge_distiller/v1/adapters/python_policy.py`。两平台均使用 python-build-standalone 20260901 的固定归档和 SHA-256。构建脚本在调用 PyInstaller 前检查真实解释器；冻结成品检查真实 `sys.executable`、版本和包内运行库清单，拒绝 3.12/3.13 等其他 ABI 的二进制。CI 使用相同补丁版本。

从全新环境分别安装 `packaging/mac-requirements-lock.txt` 和 `packaging/windows-requirements-lock.txt`。兼容解析保留原有顶层模型与依赖版本；SciPy 由要求 Python ≥3.12 的 1.18.1 改为兼容 3.11 的 1.17.1。Qwen Windows 锁重新选择 cp311 或兼容 abi3 轮子；abi3 文件中的较低 Python 标签表示兼容 ABI 下限，不代表安装了另一个 Python 解释器。

Qwen 指 Qwen3-ASR，保留原有模型 ID、固定修订及平台后端。Mac 使用 mlx-qwen3-asr 0.3.5，Windows 使用 Transformers CPU 后端。运行库版本不等于模型版本。安装器检查真实独立解释器，依赖锁改变时清空仅由安装器管理的暂存 Python 目录，保留模型下载。识别与安装共享进程锁；替换前检查旧解释器进程，搬迁后再次实际识别，失败恢复旧目录。恢复旧目录不表示允许新应用调用不符合 3.11 契约的解释器。

新环境验证成功后退役已识别旧 Python 副本和下载缓存；独有模型文件、未知文件和在用环境保留并写入 retirement.json。正式数据和系统 Python 不属于清理范围。构建环境不能指向受保护恢复源码的旧虚拟环境。

构建 `2026.09.13.3` 没有完成三环境一致性检查，其旧验收不能作为本轮证据。重发使用递增构建号与新标签，旧签名资产及标签保留可追溯性。

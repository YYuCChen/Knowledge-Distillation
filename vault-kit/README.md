# vault-kit：知识库维护工具包

本目录是 vault 侧维护文件的正本，随仓库版本管理（[决策0004](../docs/decisions/0004-phase2-alignment.md) D10）。它与仓库根目录的 AGENTS.md 无关：那一份是给开发 Agent 的工程规则，这里的 AGENTS.md 是给**维护知识库**的 Agent 的守则。

| 文件 | 安装到 vault 的位置 | 作用 |
|---|---|---|
| `AGENTS.md` | vault 根目录 | 知识库维护守则（Codex 等默认读取） |
| `tools/kb.py` | `vault/tools/kb.py` | 检查与关系图谱脚本，只用 Python 标准库，不调用 AI |

当前内容是 2026-09-29 第二次讨论交付的初版（"知识库起步包"），尚未按 [raw 接口规格](../docs/engineering/raw-interface.md)第 8 节适配。适配项包括段落引用、`raw-id` 子命令、CLAUDE.md 与快捷命令，属于二期施工任务。

安装与升级：首次由开发者手动复制到 vault，并运行 `python3 tools/kb.py init`；以后按版本升级。升级不得覆盖 vault 中用户或 Agent 已写的 wiki 内容。

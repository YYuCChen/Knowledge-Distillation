# vault-kit：知识库维护工具包

本目录是 vault 侧维护文件的正本，随仓库版本管理（[决策0004](../docs/decisions/0004-phase2-alignment.md) D10）。它与仓库根目录的 AGENTS.md 无关：那一份是给开发 Agent 的工程规则，这里的 AGENTS.md 是给**维护知识库**的 Agent 的守则。

| 文件 | 安装到 vault 的位置 | 作用 |
|---|---|---|
| `AGENTS.md` | vault 根目录 | 知识库维护守则（Codex 等默认读取） |
| `CLAUDE.md` | vault 根目录 | 一行指引，让 Claude 系工具也先读 AGENTS.md |
| `tools/kb.py` | `vault/tools/kb.py` | 检查与关系图谱脚本，只用 Python 标准库，不调用 AI；`raw-id` 子命令为 Agent 新建的自述文件取号 |
| `agent-skills/<名称>/SKILL.md` | `vault/.agents/skills/<名称>/SKILL.md` | 四个快捷命令，见下表 |

已按 [raw 接口规格](../docs/engineering/raw-interface.md)第 8 节适配：段落引用 `raw/…/编号.md#^source-N`、读取 raw 信封（身份、渠道、标题、取代）、`raw-id` 子命令、同名异义括注规则、vault 根目录 CLAUDE.md 与快捷命令。

## 快捷命令

Claudian（Codex 后端）只列出 Codex Skill，输入 `$名称` 调用；Codex 从 `<vault>/.agents/skills/` 发现它们。Claudian 只识别英文名，所以名称用英文，中文写在描述里。

| 调用 | 做什么 |
|---|---|
| `$kb-ingest` | 处理新素材：把 raw/ 中待处理的外部素材和自述写进 wiki，段落级出处 |
| `$kb-confirm` | 确认：展示待确认清单，按用户回复转正、修改或删除 |
| `$kb-lint` | 体检：程序检查加每周 AI 检查，写体检报告 |
| `$kb-read` | 陪读：和用户逐篇读一份素材，用户发言另存为 raw/自述 |

快捷命令只是操作清单，规则以 AGENTS.md 为准；其他 Agent 读 AGENTS.md 也能完成同样的事。

## 安装与升级

首次由开发者手动复制到 vault（`agent-skills/` 复制为 `.agents/skills/`），并运行 `python3 tools/kb.py init`；以后按版本升级。升级只替换上表中的文件，不得覆盖 vault 中用户或 Agent 已写的 wiki 内容，也不碰 `raw/`。

测试：`PYTHONPATH=src:. .venv/bin/python -m pytest tests/vault_kit`。端到端运行记录见 [二期交付报告](../docs/releases/phase2/delivery.md)。

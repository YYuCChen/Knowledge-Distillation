# vault-kit：知识库维护工具包

本目录是 vault 侧维护文件的正本，随仓库版本管理（[决策0004](../docs/decisions/0004-phase2-alignment.md) D10）。它与仓库根目录的 AGENTS.md 无关：那一份是给开发 Agent 的工程规则，这里的 AGENTS.md 是给**维护知识库**的 Agent 的守则。

| 文件 | 安装到 vault 的位置 | 作用 |
|---|---|---|
| `AGENTS.md` | vault 根目录 | 知识库维护守则（Codex 等默认读取） |
| `CLAUDE.md` | vault 根目录 | 一行指引，让 Claude 系工具也先读 AGENTS.md |
| `tools/kb.py` | `vault/tools/kb.py` | 检查与关系图谱脚本，只用 Python 标准库，不调用 AI；`raw-id` 子命令为 Agent 新建的自述文件取号 |
| `tools/wiki_session.py` | `vault/tools/wiki_session.py` | 在整个手动 Agent 编辑会话内持有 Vault 写锁，并向后代工具进程提供活跃会话验证 |
| `tools/wiki_display.py` | `vault/tools/wiki_display.py` | 显式规划、应用和回退既有 wiki 页的展示元数据；逐文件摘要核对，恢复证据只写到 Vault 外的私有目录 |
| `styles/kd-wiki.css` | `vault/.kd/assets/kd-wiki.css` | 已批准的阅读样式源资产；工具安装只校验并放入产品资产目录，不直接安装或启用 Obsidian snippet |
| `agent-skills/<名称>/SKILL.md` | `vault/.agents/skills/<名称>/SKILL.md` | 四个快捷命令，见下表 |

已按 [raw 接口规格](../docs/engineering/raw-interface.md)第 8 节适配：段落引用 `raw/…/编号.md#^source-N`、读取 raw 信封（身份、渠道、标题、取代）、`raw-id` 子命令、同名异义括注规则、vault 根目录 CLAUDE.md 与快捷命令。

## 快捷命令

平时直接用中文说就行：处理新素材、处理全部新素材、看看待确认、体检、陪我读这一篇。Codex 会按技能描述自动选中对应技能（2026-09-30 用 Codex CLI 实测"处理新素材"会自动读取 `kb-ingest`）。`$名称` 只是显式调用的快捷方式：Claudian（Codex 后端）输入 `$` 会列出技能，但只识别英文名，所以技能名用英文，中文写在描述里。Codex 从 `<vault>/.agents/skills/` 发现它们。

| 调用 | 做什么 |
|---|---|
| `$kb-ingest` | 处理新素材：优先处理自述及其关联材料，再补外部素材；每次一批约 5 份只是工作单元，说"全部"则逐批处理到完 |
| `$kb-confirm` | 确认：展示待确认清单，按用户回复转正、修改或删除；确认回复不另存为自述 |
| `$kb-lint` | 体检：程序检查加 AI 检查，写体检报告；首次在素材和候选都清零后运行，之后有变更时通常每周至多一次 |
| `$kb-read` | 陪读：和用户逐篇读一份素材，用户发言另存为 raw/自述 |

快捷命令只是操作清单，规则以 AGENTS.md 为准；其他 Agent 读 AGENTS.md 也能完成同样的事。

约 5 份的批次是为了让 Agent 完整阅读每份原文、在上下文充足时建立链接，并让失败可以定位到一小批；它不是总量限制。几十份存量可以用“处理全部新素材”逐批完成，Agent 在上下文边界停下时，从下一批继续即可。

## 安装与升级

首次由开发者按 `kit-manifest.json` 安装到 vault（`agent-skills/` 复制为 `.agents/skills/`）；以后按版本升级。产品在 `.kd/wiki-kit.json` 保存工具包版本及其拥有文件的摘要收据。`.kd/` 是产品自有的隐藏协议目录，不存用户正文。升级前必须验证收据中的现有摘要；用户改过的工具包文件或未登记的同名目标一律拒绝覆盖。升级只替换清单中的文件，不得覆盖 vault 中用户或 Agent 已写的 wiki 内容，也不碰 `raw/`。

工具安装、样式安装、样式启用和既有页面展示迁移是四个独立动作。安装工具不会写 `.obsidian/`，安装或启用样式也不会隐式迁移页面。展示迁移由产品在持有同一 Vault 写锁时显式调用 `wiki_display.py plan / apply / revert`；计划与备份必须在 Vault 外的私有目录。它使用逐文件原子替换和摘要比较来支持中断后重跑或回退，不是全库原子事务；用户在迁移后改过的目标会保留并报告冲突。
若既有页面已有 `[!kd-page]` 但没有产品专属 marker，迁移无法证明它属于产品，会以 `display_callout_unowned` 停止；不会自动认领、删除或再插入一份造成重复展示。

手动维护必须从会话包装器启动整个 Agent 命令，例如：

```sh
python3 tools/wiki_session.py --root . -- <agent-command>
```

Agent 开始写入前运行 `python3 tools/wiki_session.py --root . status`。状态检查会验证继承的目录锁，或向活跃包装器的本地 Unix socket 做随机 nonce 握手；只看环境变量不算持锁。包装器覆盖从首次读取、全部编辑、`kb.py` 校验到最终写回的完整窗口，不能“检查锁后释放再写”。`protocol-scan`、`--dry-run` 和 `raw-id` 是只读命令；`init` 和非 dry-run 的 `kb.py` 会拒绝无会话写入。

上面的 `python3` 是人工会话示例。产品自动任务由应用在提示词中提供应用自有的可信 helper 完整命令；Agent 必须照用，不执行 staging 内的工具副本，也不假设发行环境另有 Python。

测试：`PYTHONPATH=src:. .venv/bin/python -m pytest tests/vault_kit`。端到端运行记录见 [二期交付报告](../docs/releases/phase2/delivery.md)。

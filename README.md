# 知识蒸馏器 · Knowledge Distiller

**让读过的内容，成为下一次思考的起点。**

- 收藏一篇文章、记下一段观点之后，还能从中想到什么？知识蒸馏器从这个问题出发，希望让值得留下的材料，在回看、联系与判断中，带来新的认识。
- AI 帮你读材料、理清观点和依据、发现可能的联系；你来核查、取舍，留下自己的想法。这里积累的不只是内容，还有你如何理解它们。
- 项目当前围绕个人阅读与知识整理展开：先把来源读清楚，再把不同材料联系起来，让已有积累继续启发思考。

[下载应用](https://github.com/YYuCChen/Knowledge-Distillation/releases/latest) · [版本记录](https://github.com/YYuCChen/Knowledge-Distillation/releases) · [反馈问题](https://github.com/YYuCChen/Knowledge-Distillation/issues)

## 现在可以做什么

- **把材料留下来**：提交支持的链接、文本或文档，恢复可读原文，提炼观点、论证和对应证据，保存来源型笔记到自己的 Obsidian。
- **把内容读明白**：从观点回到依据和上下文；影响理解的听写、文字识别疑点，会交给你核对。
- **把积累联系起来**：主动点击「开始整理」，组织主题、梳理已有知识的关系，在有依据时提出新知候选。
- **留下自己的认识**：对新知选择「有点意思」或「再想想」，记录批注，以后也可以追加想法、改变判断。
- **需要时找回来**：通过主题浏览和本地文字搜索回顾材料。当前搜索用于查找已有内容，不生成问答。

作者原本的表达、AI 推导出的新知、你的个人判断，始终分开记录。新知是继续思考的邀请，不是替你下的结论；材料不足以支持新联系时，也可以不生成。

## 支持哪些材料

- **平台链接**：抖音、小红书、知乎、微博、X、YouTube、B 站中已支持的内容类型。具体获取受登录、权限和平台限制影响，并非任意页面都能处理；视频主要处理讲述内容，不分析画面。
- **文本与文档**：直接粘贴正文，或提交单个 PDF、EPUB、UTF-8 Markdown 文件；不递归导入整个目录或 Vault。
- **飞书投递**：配置自己的机器人后，可在已绑定私聊中发送链接和文字。电脑需要开机且应用运行；V1.0 不支持通过飞书投递任意文件、图片或音频。

## 下载与开始使用

- **当前公开版：V1.0**（构建 `2026.09.09.12`）。以下说明以已发布版本为准，具体更新见版本记录。
- **Mac**：[下载 Apple Silicon 安装包](https://github.com/YYuCChen/Knowledge-Distillation/releases/download/v2026.09.09.12/KnowledgeDistiller-2026.09.09.12-macOS-arm64.zip)，约 1.56GB，要求 macOS 14+。解压后将应用放入「应用程序」再打开；不支持 Intel Mac。
- **Windows**：[下载 x64 安装包](https://github.com/YYuCChen/Knowledge-Distillation/releases/download/v2026.09.09.12/KnowledgeDistiller-2026.09.09.12-Windows-x64.zip)，约 1.90GB。全部解压到可写文件夹，运行 `KnowledgeDistiller.exe`；不要只复制 EXE。已验证 Windows 10 22H2，Windows 11 与 ARM64 尚未验证。
- **首次安全提示**：Mac 当前未公证，Windows 未作发行者代码签名。请核对下载来源，按系统提示处理，不关闭整体安全防护；Mac 可参考 [Apple 官方说明](https://support.apple.com/zh-cn/102445)。GitHub 的 `Source code` 压缩包不是应用安装包。
- **首次配置**：在设置中选择已在 Obsidian 打开过的 Vault，配置模型连接，再用一段短文本试一次蒸馏。按所用功能准备 Chrome、Codex、Obsidian 及自己的账号，无需另搭 Python 环境。
- **按需启用**：处理有声内容前配置语音识别，可选本地 Qwen 或云端豆包；Qwen 需主动下载安装并保存启用。内容平台连接、飞书机器人按需配置。

应用启动后在浏览器中使用。Windows 服务窗口需保持运行；只关闭网页不会退出后台。

## 数据、费用与更新

- **本地保存**：应用记录保存在本机 SQLite；Obsidian 保存来源型笔记。AI 新知和相关个人判断当前保留在应用内，不自动写入 Obsidian，也不做双向同步。
- **本地优先不等于全离线**：已有内容浏览与搜索在本机完成；获取网络材料、使用云模型或飞书需要联网。云端处理会将任务所需材料交给对应服务，账号、额度及费用由你自行管理。
- **备份两处**：同时备份应用数据与 Vault。Mac 数据位于 `~/Library/Application Support/Knowledge Distiller/`，Windows 位于 `%LOCALAPPDATA%\Knowledge Distiller`；凭据迁移可能需要重新配置。
- **升级先看说明**：Mac 可在设置中检查更新；Windows V1.0 使用下载完整 ZIP 的手动更新方式。更新前等待任务结束、退出旧服务并备份，不删除数据或 Vault，不同时运行两个版本，不用旧版打开新版数据。

## 关于项目

- 从个人使用需求出发，持续打磨来源质量、阅读、整理与桌面体验；当前不提供团队协作、跨设备自动同步或通用知识库聊天。
- 此仓库用于介绍、发行与反馈，应用源码目前私有。第三方组件、模型与资源遵循各自许可，请保留随包声明。
- 欢迎在 [Issues](https://github.com/YYuCChen/Knowledge-Distillation/issues) 留下使用场景与问题，附上版本、系统和复现步骤；请先移除密钥、Cookie、私人材料等敏感信息。

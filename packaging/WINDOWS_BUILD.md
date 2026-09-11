# Windows 发行维护

在 Windows x64、Python 3.12.10 环境构建；用户发行包不需要这些开发工具。

1. 在工程建立 `.venv-windows`，安装 `packaging/windows-requirements-lock.txt`，再用 `pip install --no-deps .` 安装当前源码。版本锁保留现有正式依赖，不改 Mac 约束。
2. 用 `scripts/prepare_windows_models.py --manifest packaging/docling-models-manifest.json --output .windows-build/docling-models` 下载固定修订并校验全部30文件。Paddle用 `scripts/prepare_paddle_models.py --output .windows-build/paddle-models`，保留其生成清单。
3. `.windows-build/tools/bin` 放入 Windows 原生 Node、FFmpeg、FFprobe；OpenCLI1.8.7使用npm安装到 `.windows-build/tools/opencli`。来源和许可见 `Windows第三方组件.md`。不得从Mac复制运行时，不复用个人模型缓存。
4. 用 `scripts/prepare_windows_test_python.py` 创建进程UTF-8测试解释器，再运行 `scripts/windows_samples.py`、正式测试和离线样本检查。Paddle原生CPU执行器设置是本机兼容所需；应用manifest启用UTF-8解决原生库中文路径，不改变Windows全局区域设置。
5. `.venv-windows/Scripts/python.exe scripts/build_windows.py --output <新的候选目录> --version <YYYY.MM.DD.N> --product-version <产品版本> --cache-root .windows-build`。源码或资源在构建期间改变会使构建输入验收失败。构建记录含源码哈希、依赖版本与模型来源，产物只能从显式资源清单收集。
6. 对冻结目录运行 `scripts/check_windows_release.py --app <KnowledgeDistiller目录> --output <新的测试目录> --samples .windows-build/samples --documents`；继续真实GUI、只读目录、Qwen选装、ZIP解压复验。该脚本限制PATH并阻断显式诊断中的Python网络连接。
7. 记录本次候选的版本、源码提交和测试报告，再使用 `scripts/package_windows.py` 生成ZIP。许可证补充目录 `.windows-build/model-licenses` 必须存在。最终交付清单与SHA256必须在实际ZIP解压验证后生成。

测试证据必须区分正式V1、保留的未发行legacy、模拟外部服务和真实离线引擎。开发机去掉PATH不能代替干净Windows/其他硬件验收。费用和真实账号验证遵守交接约束。

# 架构导航

本页描述核对过的公开源码结构，帮助定位职责，不替代实现级合同和测试。

| 职责 | 位置 |
|---|---|
| 命令入口和数据目录 | `src/knowledge_distiller/__main__.py` |
| 应用组装与路径 | `src/knowledge_distiller/v1/app.py` |
| 持久化 | `v1/database.py`、`v1/store.py`及相关schema模块 |
| 投递路由 | `v1/intake.py`、`v1/link_intake.py`、`v1/file_sources.py` |
| 处理与核对 | `v1/worker.py`、`v1/pipeline.py`、confirmation/reviewer模块 |
| 来源适配 | `v1/`下各平台专属模块 |
| 主题与新知 | `v1/topics.py`、`v1/organization.py`及insight模块 |
| 界面 | `v1/web.py`、各表面路由、模板和静态资源 |
| 飞书 | `v1/feishu_*` |
| OCR与选装ASR | `v1/ocr.py`、`v1/vision_ocr.py`、`v1/qwen_component.py`及适配器 |
| 更新 | `v1/updates.py`、`v1/update_web.py`及打包辅助模块 |

入口导入v1.app；V1仍复用根包的primary等模块。不能因代码在v1之外或看起来年代较早就删除。

SQLite是持久化正式记录，UI消费应用状态而不维护第二套权威。外部采集和模型调用边界需要明确校验和失败反馈。平台适配保留各自支持类型、证据定位和登录限制。

Mac平台OCR使用Apple Vision；文档处理包含Docling；选装Qwen拥有独立安装生命周期。删除某个旧测试目录的模型不等于取消产品文档处理能力，也不允许删除用户正式安装组件。

公开快照包含Mac打包辅助脚本，但尚不能据此声明有完整、可复现的Windows源码构建流程。源码、原生包与实际运行证据分别报告，不因旧文档描述过通用队列/框架就重新引入。

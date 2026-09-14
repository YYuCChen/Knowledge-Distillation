# 自有页面与 Mac 桌面生命周期

V1.3 协议实现说明。原生 Chrome/Safari 成品的真实 Dock 验收另行记录；单元测试或网页 ACK 不证明用户前台已经显示目标标签。

`app.py` 继续调用 `desktop_pages.install(app)`。SSE 首次返回服务端分配的 page_id 与 connection_epoch；页面在 sessionStorage 保留 page_id，当前文档另有内存 document_id。同文档重连更新 epoch，旧流 disconnect、旧 close 和旧 ACK 均不能修改新代际。新文档握手先于旧 pagehide Beacon 到达时，服务端先为精确旧文档等待最多 0.25 秒，避免把普通导航误判成复制。复制标签携同一 sessionStorage 时，新文档在未离开旧页的情况下获分配另一个 page_id，不合并两页内容。

页面仅报告相对路由、浏览器家族提示、可见性和焦点。最近可见且聚焦或真实交互页优先，然后按稳定注册顺序回退；后台窗口虽报告 visible，但未聚焦不能抢优先级。已有页不导航或改写输入。UA 提示只能协助尽力激活浏览器，不能当进程归属或实际前台证明。

正常离开要同时收到 pagehide 与该 epoch 流终止，经过导航宽限才退役。明确的应用内导航保留预约，下一文档认领后继承原注册顺序。仅传输中断、ACK 超时、冻结不当作零页。自身 openURL 附随机 launch nonce，握手关联启动请求；原生层只在目标 bundle 有唯一可识别实例时记录 NSRunningApplication，已证实该实例终结后退役关联页。多个同 bundle 实例不能按名字猜归属。手动在其他浏览器打开的页不因家族提示相同被退役。

一次 open request 等待匹配 nonce 的页面握手，超时不循环自动新开；后续 Dock 请求复用当前状态。`ReopenOutcome` 分 opening、online、visible_reported、unknown、failed，并独立记录 received、visible、focused，foreground_verified 始终不因协议响应置真。显式另开操作用原 request_id 去重。

Mac 主循环通过 NativeReopener 排队；探测、导航宽限和握手等待均在线程内，openURL、原生激活和结果展示回到主线程。未知/隐藏 ACK/打开失败展示单例、非模态 NSAlert 窗口，提供重试显示、另开产品页面、取消，并说明旧页可能仍保留。窗口不运行网络等待或 runModal，因此正常 timer 与事件循环继续。没有引入 Apple Events、辅助功能、扩展或 WebView。

当前时序是待原生成品量测的初始值：导航宽限 2 秒、探测 1 秒、一次恢复 3 秒、打开握手 10 秒。浏览器暂停、进程退出与导航存在不可完全观察的边界；实际正常关闭矩阵必须证明能恢复，不能用 unknown 掩盖零页失败。应用内慢导航与浏览器 reload 的 0.4/2/5 秒合成服务器延迟已纳入浏览器 runner；更慢载入、bfcache、休眠及 Safari 需单独原生证据。

favicon 从既有 `packaging/assets/app-icon.png` 用系统 sips 派生 16/32/48/64/96 PNG；文件名带内容摘要，base 模板统一声明，desktop.js 以 protocol 查询参数更新旧缓存。现有静态 PNG 打包 glob 应包含资源，最终包仍须逐项读回。错误页必须由 Web Owner 继承同一 base；本模块不改 Web 错误行为。

## 验证入口

- `tests/v1/test_desktop_pages.py`：epoch、碰撞、选择、hidden ACK、迟到响应、关闭/未知、native 已证终结、握手超时去重、favicon 哈希和尺寸。
- `tests/v1/test_mac_app.py`：独立数据根、原生异步调度、主线程回调、快速点击合并、现有 Mac 邻接回归。
- `tests/v1/desktop/browser_fixture.py --data-dir <新的临时目录>`：纯合成、动态独立端口测试服务器；测试端点仅存在于该脚本。
- `tests/v1/desktop/run_browser_protocol.py --port <fixture端口> --session <独立agent-browser名称> --output <证据目录>`：页面身份、真实浏览器 ACK、慢导航/reload、复制标签和浅深图标资源。输出 parameter-lock 与逐场景状态，明确不证明 Dock/实际前台/Safari。

pytest 必须设置本 checkout 的 PYTHONPATH 和独立 basetemp。实际 Dock 需同类签名隔离 Mac 包、独立数据和受控浏览器会话；不能用工具 bringToFront 补产品结果。Windows 仅共享 Web/资源回归，原二次启动语义保持。

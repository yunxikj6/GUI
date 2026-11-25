# 云端工作台的技术选型与验证设计

第一版面向单人、单台云机。用户在本地选中一段代码时，光标和选区应当跟手；点击“运行”以后，是否真的执行、是否成功，则以云端记录为准。这两种反馈必须分别设计、分别测量。以下结论按 2026 年 11 月 25 日查阅的一手资料整理；本文中的性能数字均为实验条件或验收目标。

## 把交互放在哪一端

| 接入位置 | 云端能交付什么 | 本地能立即完成什么 | 接入代价 |
| --- | --- | --- | --- |
| 应用或代理接口 | 文件内容、会话、任务输出、工具请求、代码差异 | 已下载内容的排版、中文输入组合、选字、复制、滚动、折叠 | 需要理解应用数据，以及每个动作的执行和授权含义。VS Code 将 UI 扩展与工作区扩展放在不同位置，提供了成熟的职责划分实例。[VS Code 远程扩展架构](https://code.visualstudio.com/api/advanced-topics/remote-extensions) |
| 工具包或渲染引擎 | 绘图操作、图层、字体和图片等资源，具体取决于实现 | 以客户端分辨率重绘所支持的内容 | 得掌握目标运行时。Cloudflare 公开过在受控 Chromium 中截取并传输 Skia 指令的实现；GTK 的 Broadway 可在浏览器呈现多个兼容应用，官方也明确了后端的实验性质和新特性覆盖限制。[Cloudflare 技术说明](https://blog.cloudflare.com/cloudflare-and-remote-browser-isolation/) · [GTK Broadway](https://docs.gtk.org/gtk4/broadway.html) |
| 窗口或显示服务器 | 表面、受损区域、位图、光标，以及部分协议提供的缓存与窗口信息 | 显示现有软件，部分实现可把云端窗口交给本机窗口管理器 | 兼容面较广。Wayland 核心协议接收客户端提交的缓冲区；RFB 按矩形更新帧缓冲；RDP 图形扩展另有表面和缓存操作。不同协议在相同画面上的带宽可能相差很大。[Wayland 协议](https://wayland.freedesktop.org/docs/book/Protocol.html) · [RFC 6143](https://datatracker.ietf.org/doc/html/rfc6143) · [Microsoft RDP 图形扩展](https://learn.microsoft.com/en-us/openspecs/windows_protocols/ms-rdpegfx/5229ee1e-1cb4-4178-9739-a36f1258b685) |

“本机能画出一个字形”和“本机知道它属于哪个输入框、怎样选中它”是两个不同的条件。字体和图层传输可以改善清晰度，编辑仍需要文本模型、光标位置和输入法组合状态。浏览器里承载远程窗口，也与操作系统里拥有独立窗口有区别。Xpra 的原生 seamless 客户端值得用来测试后者；它可以在专属会话启动程序，把原有桌面拿来共享则属于 shadow 模式。[Xpra 窗口模式](https://github.com/Xpra-org/xpra/blob/master/docs/Usage/Seamless.md)

项目已用原生 GTK 4 控件和 Chromium 验证这一边界。三次回环测试中，浏览器接收到了可操作的窗口，GTK 输入框获得完整选区，而浏览器原生文本选区始终为空。实验记录包含版本、完整输入序列及双向协议观测。[GTK Broadway 实验记录](toolkit-experiment.md)

后续用同一 GTK 3 输入框对照了原生 X11、工作台逐帧、Broadway 和 TigerVNC。四条路径都能选中同样的 7 个字符。另一个 GTK 进程提供“中文✓”后，三条 X11 路径均粘贴到这段新文字，Broadway 中原输入框三次都粘贴到自己上次复制的文字。工作台实时查看器现已用 TigerVNC 在云机 GTK 与本机系统剪贴板间验证了用户主动的双向传递。[四种路径及实机验收](validation-desktop-clipboard.md)

## 首版的取舍

1. **从单次代理任务起步。** Codex 的非交互模式提供 JSON 行事件，Claude Code 的无头模式也能发结构化输出。两者可以落到同一份“任务状态和输出”记录上。原始事件的粒度不同，CLI 也可能升级；任务退出、代理实际失败、工具被拒绝应分别处理。测试时至少区分“找到了可执行文件”“认证可用”“完成真实模型任务”。[Codex 非交互模式](https://developers.openai.com/codex/noninteractive/) · [Claude Code 无头模式](https://code.claude.com/docs/en/headless)
2. **为完整会话选择双向协议。** Codex App Server 有 thread、turn、审批和恢复接口，Claude Agent SDK 允许应用参与工具授权。升级时须保存代理自己的会话 ID、待处理审批 ID 和用户的决定。界面只允许审批结果匹配原来的请求，断线期间待决定的操作继续等待。公开的 Agent Client Protocol（ACP）也是多代理接入候选，先验证所需适配器对恢复、取消、差异和审批的支持，再决定哪些部分可以共用。[Codex App Server](https://developers.openai.com/codex/app-server/) · [Claude SDK 权限](https://code.claude.com/docs/en/agent-sdk/permissions) · [ACP 协议](https://agentclientprotocol.com/protocol/v1/overview)
3. **桌面入口保留完整 Linux 环境。** InstaBox 桌面支持 VNC over WebSocket，能为尚未适配的程序提供真实操作入口和同机性能对照。文件和差异控件由本机绘制，任意 Linux 窗口的逐窗转发另行验证。云端突然出现文件选择器、中文候选框、模态浮窗时，它们的父子关系、焦点和坐标都必须正确。[InstaBox 桌面接口](https://instabox.sh/docs/access/desktop) · [Xpra 窗口模式](https://github.com/Xpra-org/xpra/blob/master/docs/Usage/Seamless.md)
4. **网络中断与云机休眠分开处理。** UI 提交时生成请求 ID；服务端应在启动工作前持久化请求与任务的映射，输出按任务递增编号。断线补读以最后确认的编号为准。未来使用 SSE 推送时仍需保存事件，接收端虽能发送 Last-Event-ID，历史数据由应用负责。网络中断时远端任务可以继续；云机暂停时进程冻结；彻底停止再启动时还须能识别旧进程已消失。[HTML Server-Sent Events](https://html.spec.whatwg.org/multipage/server-sent-events.html) · [InstaBox 生命周期](https://instabox.sh/docs/machines/auto-pause)

## 能推翻选型的实验

同一台云机、相同客户端分辨率和缩放、同一份 UTF-8 中文代码、同一次静态资源缓存状态，分别测量本地数据界面和 Linux 桌面。云端暂停后的首次启动、已运行但缓存为空、重复打开三种情形分开记录。先测无网络整形的现场值，再分别引入 20、80、150 毫秒往返延迟和 0%、1% 丢包；这些参数只用于构造不同条件。每个交互条件争取收集 100 次样本并留存原始值。

| 工作负载 | 记录 | 建议判据与后续动作 |
| --- | --- | --- |
| 输入一行中文，滚动已经加载的长差异，缩放到 200% | 本地输入到下一次绘制的 p50/p95、额外网络请求数、滚动长帧、截图及实际选中文本 | 提议本地操作 p95 低于 50 毫秒，网络条件改变时仍稳定；若滚动引起持续网络请求，先修缓存和渲染。浏览器计时标注观察边界，必要时用高速摄影核对实际显示，浏览器时长不等于显示器发光时间。[W3C Event Timing](https://www.w3.org/TR/event-timing/) |
| 重放同一段已录制任务事件，另外执行一次真实代理任务 | 同一客户端时钟上记录提交、确认和首个可见输出；云端另记排队、启动和产出时长；记录事件批次数、传输字节和两个端点 CPU | 固定事件回放可排除模型生成速度的变化。若高分位延迟主要来自多次云 API 调用和轮询等待，优先实验服务端批读、长轮询或持久推送，再决定传输方式。跨机器的单向时延要提供时钟误差或使用往返对照。 |
| 提交后立即断网，服务端已接受但客户端尚未收到响应；随后刷新并重连 | 远端启动次数、请求与任务的映射、事件顺序、缺失和重复、草稿状态 | 同一个请求只启动一个任务，重试后能找到原任务。补读的事件无遗漏且每条只出现一次。另做云机暂停/恢复与云端进程终止试验，终止要产生可恢复或明确失败的状态。 |
| GTK 文本编辑器加文件对话框、图片查看器、快速滚动的混合页面 | 首窗耗时、对话框焦点、输入确认、100% 和 200% 缩放的文字边缘、屏幕稳定后的字节数及操作期间峰值 | 先比较完整桌面与 Xpra 单窗。只有在相同输入、清晰度和正确性下仍有明确瓶颈，才值得投入受控 GTK 或 Chromium 引擎。资源同步、字体许可、GPU/视频表面和跨版本测试一并计入维护成本。 |

## 扩展时保留的接口

云主机接入负责发现机器、唤醒、安装兼容版本的工作进程和访问图形桌面。应用接入负责声明可提供的能力，如只读任务、可续会话、审批、文件预览或独立窗口。新增第二个主机后端时，复用同一组文件、任务重试、断线补读测试；新增第二个富交互应用时，用同一组待审批、取消、恢复和界面退化测试。先用两种实现找出真正相同的行为，再稳定对外协议。

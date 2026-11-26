# Cloud Linux Workspace

在 Windows 解压客户端并双击 `Start.cmd`。配对后先看到原版 Ubuntu 桌面，点击 Dock 上的 ChatGPT 会在云端启动 OpenAI 的 Linux 程序。Windows 随包提供绘图运行时，在本地窗口执行 GNOME 和应用生成的绘图指令。云端保有会话、控件焦点和输入状态。[下载 Windows x64 客户端](releases/original-win64/cloud-workspace.zip) · [下载云端服务](releases/ubuntu-source/cloud-workspace-server.zip)

**第一次连接**需要在云机启动绘图服务。在装好依赖的 Ubuntu 24.04 云机上，解压云端服务，进入 `cloud-workspace-server` 目录，运行以下命令。终端会提供本次会话的配对码。在 Windows 输入该码和 InstaBox 密钥即可连接。使用 Debian 13 的云机有单独的安装入口。[云服务器准备](cloud_render/provision/README.md)

```sh
python3 -m experiments.original_desktop_source --port 17326
```

当前主入口已验证原版 ChatGPT 欢迎页、登录方式和登录前表单输入。云端应用使用临时配置和隔离网络。持久登录、对话、Claude 的 Artifacts 与实体 Windows 11 上的体验仍待验证。[Win64 窗口实拍](docs/measurements/original-editing/input.png) · [桌面和应用的操作记录](docs/interactive-original-desktop.md) · [断网后在全新本机图形上下文重绘的测量](docs/measurements/original-desktop-local-redraw.json)

## 连接基础 Ubuntu 桌面

只使用 GNOME 桌面、概览搜索和独立应用通道时，可以选用同时支持多种桌面分辨率的基础客户端。[下载基础桌面客户端](releases/thin-windows/cloud-workspace.zip)

如果你的 InstaBox 是 Debian 13 amd64，可在云机的本仓库目录运行 `python3 -m cloud_render.provision`，由云机校验 Ubuntu 官方软件包，启动一次性桌面。终端显示端口后即可配对，关闭该命令会回收本次 Ubuntu 环境。[Debian 云机的空间、启动与回收](cloud_render/provision/README.md)

在已具备 Ubuntu 桌面依赖的云机上运行 `python3 -m cloud_render.gnome_relay --port 17325`。在 Windows 解压完整客户端，打开 `Start.cmd`，输入 InstaBox 密钥和本次桌面配对码。服务仅监听云机回环，连接使用 InstaBox 的私有 HTTPS 通道。本机窗口支持指针移动、左右点击、导航键和 Ctrl+Esc 活动概览。可用 `--desktop-size 1920x1080` 请求全高清桌面，按默认参数启动时使用 800×520。Ubuntu 官方壁纸作为静态资源随包提供，匹配到相同源纹理时通过摘要引用。x64 使用内置运行时。ARM64 需要 Windows 11 x64 仿真，实机兼容性待验证。

下图来自发布包的实际窗口。双击启动的 Win64 程序通过 InstaBox 私有 HTTPS 连接原版 Ubuntu。鼠标点开快速设置后，本机窗口的 800×520 个像素与云端第 84 帧完全一致，并显示 “Dark Style”。按 Escape 后，菜单消失，窗口与第 125、126 帧相同。这次验证运行在 Wine 10，物理 Windows 11 仍待验证。[正式包的启动、操作和逐帧记录](docs/measurements/ubuntu-windows-launch.json) · [较早会话的结果](docs/gnome-command-stream.md)

![正式 Windows 包在云端 Ubuntu 桌面打开快速设置](docs/ubuntu-windows-launch-settings.png)

## 从 Ubuntu Dock 打开原版 ChatGPT

应用桌面有独立的 Win64 客户端和云端服务。进入桌面后点击 Dock 中的 ChatGPT 图标，云端启动原版程序。本机用 CanvasKit 绘制程序提供的 Skia 指令，再由 Win64 VirGL 按 GNOME 的指令将窗口合成到桌面中。已在隔离的 Ubuntu 与 Win64 客户端之间验证点击欢迎页、打开登录方式。此入口需要云端安装指定版本的 ChatGPT 和绘图依赖。[桌面应用的启动与实拍](docs/interactive-original-desktop.md)

完整云桌面还支持在原版概览搜索框中使用 Windows 输入法。候选词在本机选择，提交后云端原搜索框更新文字和结果。已在原控件验证中文、退格、全选替换、取消和失焦，并与独立读取的 Win32 窗口核对像素。[原生输入法验收](docs/gnome-search-ime.md)

## 单独打开云端 ChatGPT

单独连接云端的 ChatGPT 绘图服务时，在 Windows 运行 `Start.cmd --cloud-app`。服务从原版 ChatGPT for Linux 提取 Skia 指令。本机的 Microsoft Edge 使用随包 CanvasKit 绘制原始界面，云端应用负责编辑状态、输入处理和账号。两条服务使用各自的配对码。[云端应用的安装与启动](docs/thin-client.md)

在真实云机上验证了欢迎页、登录方式菜单、测试文本输入、中文输入法提交、删除和取消返回。本机与云端独立截图取自同一个原厂窗口。已测试的 11 个操作均得到确认，一次测量的操作至下一画面中位数为 0.751 秒。登录后聊天和 Claude Artifacts 仍待验收。[本机原厂界面](docs/cloud-render-window.png) · [本机中英文输入](docs/cloud-render-input.png) · [云端原窗口](docs/cloud-render-input-source.png) · [传输与交互数据](docs/measurements/cloud-render.json)

GNOME 可以把普通应用已经绘好的整窗纹理作为资源上传。桌面客户端只接入有绘图来源验证的界面。应用在 Ubuntu 窗口内的原厂绘制需要逐个接入，已经在两个登录前状态验证了原厂页面和原厂标题栏的本机合成。目前默认桌面中的普通应用入口会提示绘图通道尚未就绪。下列记录给出了同一个原厂窗口的画面对照和在 Windows 上持续接收原始绘图的验证结果。[原厂界面与窗口合成实测](docs/application-surface.md) · [连续绘图的测量](docs/incremental-application-surface.md)

## 本机重绘原版 Claude

从 Claude Desktop 自带的开发者工具导出绘图时，可在云源核对它所属的主窗口和图层，再在本机 Win32 窗口重绘原厂文字、按钮和路径。已实测原版离线界面。实时交互、登录会话和 Artifacts 仍待接入。[Claude 原版窗口的导出、打开与测量](docs/claude-original-drawing.md)

## 显式兼容入口

`Start.cmd --native` 会在用户已经安装的本机 Ubuntu WSLg 内打开 GNOME。`Setup.cmd` 专用于这种模式。远端 Xfce 整屏和原有网页项目工作台分别使用 `python launcher.py --cloud-desktop`、`python launcher.py --workbench`。旧模式的桌面与官方客户端实拍保存在各自的验收记录中。[本地 WSLg 模式](docs/native-windows.md) · [云桌面与项目工作台](docs/remote-workbench.md)

### 兼容模式的项目文件

在“活动”里打开 **InstaBox 项目文件**，首次使用时选择云机和项目目录。选中的云端文本会出现在 Ubuntu 文件管理器里。双击即可用本机编辑器打开。Ctrl+S 保存到本机，在文件上右键选 **脚本 → 保存到 InstaBox** 才会更新云端。实际云机已独立核对编辑后的字节。[首次连接与使用方法](docs/native-project-files.md) · [编辑器实拍](docs/native-cloud-file-edit.png)

在 GNOME 的“活动”里搜索 **连接 InstaBox 到官方客户端**，选择 Claude 或 ChatGPT 的本地 Codex 项目，再确认云机与项目目录。首次使用会引导你选择项目，并按需准备连接组件。重新打开所选客户端后，请从其 MCP 设置查看 `instabox-workspace`。默认可以列目录、读文本和查询 Git 状态。选择“包含 Git 差异”后，也可以在原厂客户端里问“这个云项目最近一次提交改了什么”，或查看尚未提交的代码。账户、聊天记录和工具授权仍由厂商管理。[连接步骤与权限](docs/official-client-workspace.md)

# Win64 原厂应用云桌面

解压完整的独立 x64 客户端，在 Windows 双击 `Start.cmd`。输入 InstaBox API 密钥和本次云桌面配对码后会打开原生 Windows 窗口。看到 Ubuntu 桌面后，自己点击 Dock 里的 ChatGPT，在原版登录页点击 “Sign in another way”，再选择实际需要的登录方式。原厂应用向云源发布的新绘图会继续显示在同一个 1280×900 桌面。聚焦已识别的原厂编辑框后，可使用 Windows 键盘输入文字，Ctrl+A 选中该框内容。窗口与云端都在运行时可持续移动鼠标和左键点击，关闭窗口会断开会话。连接在出图后中断时，窗口会保留最近完成的本地画面，并在标题栏标明只读。此时缩放窗口仍会重绘已保留的画面，各类输入停止发送。

本地包包括应用绘图引擎、Win64 VirGL、Python 和 Node.js。原版 Ubuntu、ChatGPT、用户资料与输入处理在云服务器。本机的 CanvasKit 执行原厂页面、标题栏和按钮的 SkPicture，Win64 VirGL 按原有 GNOME 命令合成画面。尚未得到原厂绘图的应用提交会暂停呈现。其他软件与窗口状态、实体 Windows 11 上的输入法与设备仍需独立验证。

## 云端启动

在已安装原版 Ubuntu、ChatGPT、GNOME 和见证依赖的云机上，从仓库目录运行一次下面的命令。启动时在私有终端输入已有的 43 位随机配对码，本机客户端使用同一码。自动化部署可由密钥管理器注入环境变量 `CLOUDWORKSPACE_GNOME_TOKEN`。

```sh
python3 -m experiments.original_desktop_source --port 17326 --lifetime 3600
```

使用 `--port 0` 会在终端报告本次分配的回环端口。默认一小时包括启动和等待配对的时间。关闭已配对的窗口、终止这条命令或达到时限后，云机会回收该会话的进程与临时目录。再次运行会创建新的桌面。

要在下次配对时继续使用相同的原厂云端应用数据，可在每次启动时指定同一目录。

```sh
python3 -m experiments.original_desktop_source --profile "$HOME/.local/share/cloud-workspace/chatgpt-desktop"
```

首次启动会创建权限为 `0700` 的专用目录，也可指定已有的私有空目录。一个目录同时只供一个桌面会话使用。关闭后应用自行写入的配置和数据保存在这个云端目录，下一次仍需重新配对。省略 `--profile` 时应用配置随临时会话回收。此入口的图形进程仍在隔离网络中运行。

配对桥只在云机回环监听。使用 InstaBox 的 Windows 客户端通过现有官方私有传输取原版指令、绘图与真实输入确认。已将同一云端回环端口私密转发到本机的环境也可运行下列命令。配对码只通过交互提示或 `CLOUDWORKSPACE_GNOME_TOKEN` 输入。

```bat
Start.cmd --forwarded --port 17326
```

## 打开云端系统浏览器的原版视口

完整桌面使用上面的默认入口。要单独操作 Chromium 自己绘制的离线错误页，在有图形会话、GIO、原版 Chromium 和 `requirements-render.txt` 中依赖的云机上，进入解压的云端服务目录再运行以下命令。默认可执行文件为 `/usr/lib/chromium/chromium`。

```sh
python3 -m cloud_render.browser_viewport_service --port 17328
```

云端会给本次视口单独生成配对码。Windows 解压完整 x64 客户端，在其目录内运行以下命令，并输入该配对码。已自行建立私有回环转发时，省略 `--instabox`。

```bat
Start.cmd --browser-viewport --instabox --port 17328
```

云端用系统默认处理器打开真实 Chromium 窗口。Windows 收到浏览器内容区的原始 Skia 绘图，在独立原生窗口本地执行。此入口使用固定无查询参数的离线页面，用于验证窗口、绘图和输入。原厂账户登录、浏览器地址栏和标签栏在完整桌面中的显示仍需接入。

## 用户操作验证

独立解压的 Windows 客户端经配对桥进入本次真实 Ubuntu。用户从原版 Dock 点击 (32,66) 启动 ChatGPT，再在欢迎页点击 (672,502)。云端依次确认四次真实移动和左键。窗口在第二次点击后约 4.57 秒显示登录方式，随后 Alt+F4 正常关闭本机窗口，云端收到了控制连接 EOF。该次启动使用包内的 Windows Python、Node 和渲染库。操作过程中未向客户端传入预录的提交表或截图。[初始桌面](measurements/original-window/manual-desktop.png) · [应用欢迎页](measurements/original-window/manual-before.png) · [登录方式](measurements/original-window/manual-after.png)

## 实测范围

同一次隔离云源角色中，原版 ChatGPT 的欢迎页随 Windows 窗口的一次实际左键打开登录方式。云端独立确认投递坐标为 (672, 502)。Windows 端在 1.689 秒后收到了新的原厂绘图，第一次显示欢迎页和菜单分别落在第 75 和第 83 次扫描。90 次扫描有 50 次等待原厂提交配对，Win32 实际发生 38 次绘制。源端去除了 97,838,400 字节的应用栅格，向本机交付了两份不同的原厂绘图。

这一轮的云角色是隔离的 Ubuntu 软件环境，显示端是 Wine 上运行的 Windows x64 程序。源端关闭了空桌面的可选 DING 图标扩展。真实桌面和客户端的鼠标事件、渲染与窗口拍摄同时在线完成。测试观察器用 OCR 确认截图，用户启动入口直接处理 Windows 的鼠标及键盘输入。图中鼠标覆盖了 “Bedrock” 左侧的一个字符。[测量](measurements/original-window/session.json) · [欢迎页](measurements/original-window/before.png) · [登录方式](measurements/original-window/after.png)

官方 Windows Node.js 24.22.3 的 ZIP 和 EXE 各自通过官方 SHA-256 校验，随包包含 Node 上游的完整许可证。[Node.js 发布摘要](https://nodejs.org/dist/v24.22.3/SHASUMS256.txt)

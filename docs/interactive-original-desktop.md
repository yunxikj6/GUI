# Win64 原厂应用云桌面

解压完整的独立 x64 客户端，在 Windows 双击 `Start.cmd`。输入 InstaBox API 密钥和本次云桌面配对码后会打开原生 Windows 窗口。看到 Ubuntu 桌面后，自己点击 Dock 里的 ChatGPT，在原版登录页点击 “Sign in another way”，再选择实际需要的登录方式。原厂应用向云源发布的新绘图会继续显示在同一个 1280×900 桌面。聚焦已识别的原厂编辑框后，可使用 Windows 键盘输入文字，Ctrl+A 选中该框内容。窗口与云端都在运行时可持续移动鼠标和左键点击，关闭窗口会断开会话。连接在出图后中断时，窗口会保留最近完成的本地画面，并在标题栏标明只读。此时缩放窗口仍会重绘已保留的画面，各类输入停止发送。

本地包包括应用绘图引擎、Win64 VirGL、Python 和 Node.js。原版 Ubuntu、ChatGPT、用户资料与输入处理在云服务器。本机的 CanvasKit 执行原厂页面、标题栏和按钮的 SkPicture，Win64 VirGL 按原有 GNOME 命令合成画面。尚未得到原厂绘图的应用提交会暂停呈现。其他软件与窗口状态、实体 Windows 11 上的输入法与设备仍需独立验证。

## 云端启动

在已具备项目说明所需的原版软件、GNOME 及见证依赖的 Ubuntu 云源中运行。下面的命令生成新的专用目录和配对码。配对码须通过你的私有渠道交给本机。

```sh
umask 077
work=$(mktemp -d)
export CLOUDWORKSPACE_GNOME_TOKEN=$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')
bash experiments/mutter_app/run.sh --interactive "$work/pair" "$work/source" &
python3 -m experiments.original_desktop_bridge "$work/pair" --port 17326
```

配对桥只在云机回环监听。使用 InstaBox 的 Windows 客户端通过现有官方私有传输取原版指令、绘图与真实输入确认。已将同一云端回环端口私密转发到本机的环境也可运行下列命令。配对码只通过交互提示或 `CLOUDWORKSPACE_GNOME_TOKEN` 输入。

```bat
Start.cmd --forwarded --port 17326
```

## 用户操作验证

独立解压的 Windows 客户端经配对桥进入本次真实 Ubuntu。用户从原版 Dock 点击 (32,66) 启动 ChatGPT，再在欢迎页点击 (672,502)。云端依次确认四次真实移动和左键。窗口在第二次点击后约 4.57 秒显示登录方式，随后 Alt+F4 正常关闭本机窗口，云端收到了控制连接 EOF。该次启动使用包内的 Windows Python、Node 和渲染库。操作过程中未向客户端传入预录的提交表或截图。[初始桌面](measurements/original-window/manual-desktop.png) · [应用欢迎页](measurements/original-window/manual-before.png) · [登录方式](measurements/original-window/manual-after.png)

## 实测范围

同一次隔离云源角色中，原版 ChatGPT 的欢迎页随 Windows 窗口的一次实际左键打开登录方式。云端独立确认投递坐标为 (672, 502)。Windows 端在 1.689 秒后收到了新的原厂绘图，第一次显示欢迎页和菜单分别落在第 75 和第 83 次扫描。90 次扫描有 50 次等待原厂提交配对，Win32 实际发生 38 次绘制。源端去除了 97,838,400 字节的应用栅格，向本机交付了两份不同的原厂绘图。

这一轮的云角色是隔离的 Ubuntu 软件环境，显示端是 Wine 上运行的 Windows x64 程序。源端关闭了空桌面的可选 DING 图标扩展。真实桌面和客户端的鼠标事件、渲染与窗口拍摄同时在线完成。测试观察器用 OCR 确认截图，用户启动入口直接处理 Windows 的鼠标及键盘输入。图中鼠标覆盖了 “Bedrock” 左侧的一个字符。[测量](measurements/original-window/session.json) · [欢迎页](measurements/original-window/before.png) · [登录方式](measurements/original-window/after.png)

官方 Windows Node.js 24.22.3 的 ZIP 和 EXE 各自通过官方 SHA-256 校验，随包包含 Node 上游的完整许可证。[Node.js 发布摘要](https://nodejs.org/dist/v24.22.3/SHASUMS256.txt)

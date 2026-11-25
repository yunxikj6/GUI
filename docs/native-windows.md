# Windows 上的本机 Linux 工作区

解压整个源码包并双击 `Start.cmd`。启动器会调用 Windows 系统的 `wsl.exe`，在已安装的本机 Ubuntu 中打开一个 GNOME Shell 窗口。这个窗口里进行的桌面搜索、菜单绘制和文件操作发生在本机。用户可以在未连接 InstaBox 的情况下使用桌面。Windows 主入口附带 x64 和 ARM64 两个程序，随包提供 Go 源码。两个程序当前均未使用 Authenticode 签名；可以对照验收文件中的 SHA-256，或用 Go 1.26.11 和 `windows/launcher/build.sh` 重建。[启动器源码](../windows/launcher/)

## 第一次准备

1. 在 Windows 11 上运行 `wsl --version` 和 `wsl --list --verbose`，确认有 WSLg，并且要用的 Ubuntu 是版本 2。还没有 Ubuntu 时，可按微软说明安装，例如 `wsl --install -d Ubuntu-24.04`。首次启动发行版时完成 Linux 用户设置。[微软 WSLg 文档](https://learn.microsoft.com/windows/wsl/tutorials/gui-apps)
2. 双击 `Setup.cmd`。安装程序在交互终端列出所选 Ubuntu 24.04 LTS 缺少的桌面组件，通过该发行版已有的 APT 源安装。Ubuntu 的提权提示也在这个终端输入。有多个 Ubuntu 时可运行 `Setup.cmd --distro Ubuntu-24.04`。已具备依赖的机器可以直接打开工作区。
3. 在 Windows 解压目录运行 `Start.cmd --check-native` 检查依赖，再双击 `Start.cmd`。存在多个 Ubuntu 时可指定 `Start.cmd --distro Ubuntu-24.04`。启动时会在当前 Ubuntu 用户的“活动”中注册应用入口，并在启用了桌面图标时首次建立快捷方式。可以移动或删除图标。以后解压到新目录并运行新版启动器，现有快捷方式仍可使用。

若想从 Ubuntu 终端自行安装，同一套软件包如下。

```sh
sudo apt update
sudo apt install --no-install-recommends gnome-shell ubuntu-session ubuntu-settings gnome-shell-extension-ubuntu-dock \
  gnome-shell-extension-appindicator gnome-shell-extension-desktop-icons-ng \
  yaru-theme-gtk yaru-theme-icon ubuntu-wallpapers-noble fonts-ubuntu nautilus gvfs \
  xdg-user-dirs dbus-daemon x11-utils xwayland libgl1-mesa-dri \
  gnome-text-editor zenity gnome-keyring python3-keyring libnotify-bin fonts-noto-cjk ca-certificates
```

## 打开原版应用

- **Claude**。在 Ubuntu 安装 Anthropic 官方 `claude-desktop` 软件包。在 GNOME 的“活动”内搜索 Claude，然后按 Enter。Linux 客户端使用自己的窗口和账户。若 Ubuntu 没有安装它，桌面提供 Microsoft Store 版 Claude 的启动入口。独立安装的 Windows 版也可从 Windows 开始菜单打开。[Anthropic 官方安装说明](https://support.claude.com/en/articles/10065433-install-claude-desktop)
- **ChatGPT**。通过 OpenAI 的下载入口在 Microsoft Store 安装官方桌面应用。在 GNOME 的“活动”内搜索 ChatGPT 并打开 Windows 版。窗口交由 Windows 管理，可以用 Windows 任务栏或 Alt+Tab 切换；它不会出现在 Ubuntu 的窗口概览里。[OpenAI 官方下载](https://chatgpt.com/download/)
- **项目数据**。需要使用 InstaBox 时，为对应的官方客户端配置工作区连接。云端文件路径、Git 状态和保存结果来自所选云机。Claude 的聊天历史及 Artifacts、ChatGPT 的聊天历史继续受各自的账号与功能权限控制。[连接与验证方法](official-client-workspace.md)
- **在 Ubuntu 直接编辑项目文件**。完成绑定后，在“活动”打开 InstaBox 项目文件，从云端选一个文件，双击本地工作副本即可用系统编辑器修改。客户端会话和文件管理器可以独立使用。[原生文件操作](native-project-files.md)

在有图形会话的 Linux 上，可用 `python3 launcher.py --check-native` 检查前置条件，再运行 `python3 launcher.py` 启动本地会话。已有远端 Xfce 和浏览器项目工作台分别从 `--cloud-desktop` 与 `--workbench` 进入。

Ubuntu 24.04 的 GNOME Shell 46 启动时读取父显示的可用区域，给桌面边框和任务栏留出空间。Linux 嵌套窗口实测在 1920×1080 显示上得到 1888×1016 的桌面，在 1280×720 上得到 1248×656。这个版本的 Mutter 在运行中固定嵌套窗口大小，更换显示后可关闭再打开。Windows 的显示缩放仍需实机验证。[逐项测量](measurements/native-gnome-window-size.json)

当 WSLg 把父 X11 套接字目录挂为只读时，工作区使用 Wayland 运行 Ubuntu 内的应用。已在该条件下打开 Nautilus 和官方 Claude。仅支持 X11 的 Linux 程序需要可用的嵌套 Xwayland。

## 在 Windows 上验证桌面窗口

完成首次准备后，在 Windows 11 的解压目录打开 PowerShell，运行以下命令。有多个 Ubuntu 时，在末尾加上 `-Distro Ubuntu-24.04` 或你注册的完整名称。

```powershell
powershell.exe -NoProfile -File .\windows\Test-NativeWorkspace.ps1
```

1. 脚本通过 `Start.cmd` 启动新桌面。Ubuntu 24.04 日常窗口的标题是 **Cloud Linux Workspace**。这次验收的窗口标题另带一串方括号标识，用来区分已打开的其他桌面。
2. 等 Ubuntu 文件管理器打开名为 **Files** 的目录，点击窗口使它位于前台。看到 Ubuntu 桌面通知后，按 **Ctrl+Shift+N**，输入通知中的 `Verified-xxxxxxxx` 名称，按 Enter。让工作区保持在前台，直到验证结束。默认操作时间为 180 秒，可用 `-TimeoutSeconds` 调整。
3. PowerShell 最后一行是 JSON。`status` 为 `verified` 且退出码为 0，表示本次运行观测到同一个 Windows 前台窗口、对应的 Ubuntu GNOME 和 Nautilus 进程，以及刚创建的空目录。报告有窗口句柄、宿主进程、Ubuntu 版本和两个 Linux 进程号，可留作该机器的验收记录。若失败，退出码为 1，查看 `phase` 和 `errorCode`。例如 `foreground-not-observed` 表示创建前后的前台窗口采样不完整。

命令使用本机临时目录，无需登录厂商账户或连接 InstaBox。验收结束后桌面继续运行，脚本清理它识别的临时目录。若向目录里添加了其他文件，`cleanup` 会显示 `preserved` 并保留内容。验收仅采样前台和文件系统结果，操作仍需由人在文件管理器内完成。[宿主验收脚本](../windows/Test-NativeWorkspace.ps1) · [Ubuntu 实测原图](native-ubuntu-files-verification.png) · [进程与文件记录](measurements/native-windows-verifier.json)

## 官方客户端验收

- 在 Ubuntu 24.04.5 的 GNOME Shell 46.0 上，使用 Dock 中的原版文件管理器新建了目录并核对了磁盘结果。随后通过“活动”启动了 Anthropic 官方签名的 Claude Desktop 6.28899.6。程序映像与签名软件包中的文件摘要相同。[Ubuntu 桌面截图](native-ubuntu-desktop.png) · [Claude 窗口截图](native-ubuntu-claude.png)
- 退出 Claude 后，双击桌面图标会重新打开客户端。登录前就可以展开原厂菜单，实测包含 File、Edit、View、Go、Help。[桌面图标打开的 Claude 菜单](native-ubuntu-claude-menu.png)
- 在只有回环接口的 Ubuntu 环境中，用全新用户配置双击启动官方 Claude。窗口显示 `ERR_INTERNET_DISCONNECTED`，可以展开 File 菜单，看到 Settings、Close Window、Quit。完整实拍含 Linux 窗口管理器绘制的 Cloud Linux Workspace 标题栏。[离线整屏原图](native-ubuntu-branded-claude.png) · [运行中程序摘要](measurements/native-windows-verifier.json)
- 客户端保持沙盒与 TLS 校验，验收到公开欢迎页。登录后的侧栏、聊天记录及 Artifacts 仍需获授权账户验收。
- Windows 启动程序已按 x64、ARM64 构建并检查 PE 的 GUI 子系统。当前测试机是 Linux。Windows 11 上的实际 WSLg 窗口、真实 ChatGPT 窗口及端到端焦点切换尚待真机验证。

在已登录的 Windows 桌面运行以下命令。脚本核对当前用户的 Microsoft Store 应用身份，调用日常入口使用的启动程序，再检查可见窗口。

`activationPid` 是 Windows 接受激活请求时返回的进程号。`after` 记录可见窗口的进程和包身份。`observation` 区分新窗口、已有窗口移到前台和原有窗口持续可见。报告省略窗口标题和聊天内容。

```powershell
powershell.exe -NoProfile -File .\windows\Test-OfficialAppWindow.ps1 -App ChatGPT -TimeoutSeconds 60
powershell.exe -NoProfile -File .\windows\Test-OfficialAppWindow.ps1 -App Claude -TimeoutSeconds 60
```

# Windows 上的本机 Linux 工作区

解压整个源码包并双击 `Start.cmd`。启动器会调用 Windows 系统的 `wsl.exe`，在已安装的本机 Ubuntu 中打开一个 GNOME Shell 窗口。这个窗口里进行的桌面搜索、菜单绘制和文件操作发生在本机。用户可以在未连接 InstaBox 的情况下使用桌面。Windows 主入口附带 x64 和 ARM64 两个程序，随包提供 Go 源码。两个程序当前均未使用 Authenticode 签名；可以对照验收文件中的 SHA-256，或用 Go 1.26.11 和 `windows/launcher/build.sh` 重建。[启动器源码](../windows/launcher/)

## 第一次准备

1. 在 Windows 11 上运行 `wsl --version` 和 `wsl --list --verbose`，确认有 WSLg，并且要用的 Ubuntu 是版本 2。还没有 Ubuntu 时，可按微软说明安装，例如 `wsl --install -d Ubuntu-24.04`。首次启动发行版时完成 Linux 用户设置。[微软 WSLg 文档](https://learn.microsoft.com/windows/wsl/tutorials/gui-apps)
2. 双击 `Setup.cmd`。安装程序在交互终端列出所选 Ubuntu 24.04 LTS 缺少的桌面组件，通过该发行版已有的 APT 源安装。Ubuntu 的提权提示也在这个终端输入。有多个 Ubuntu 时可运行 `Setup.cmd --distro Ubuntu-24.04`。已具备依赖的机器可以直接打开工作区。
3. 在 Windows 解压目录运行 `Start.cmd --check-native` 检查依赖，再双击 `Start.cmd`。存在多个 Ubuntu 时可指定 `Start.cmd --distro Ubuntu-24.04`。启动时会在当前 Ubuntu 用户的“活动”中注册应用入口，并在启用了桌面图标时首次建立快捷方式。可以移动或删除图标。以后解压到新目录并运行新版启动器，现有快捷方式仍可使用。新版创建的快捷方式会在下次启动工作区时更新应用图标。旧版创建的桌面文件保留原有图标和启动权限，切换到 Linux 版后会打开对应的本机客户端。

若想从 Ubuntu 终端自行安装，同一套软件包如下。

```sh
sudo apt update
sudo apt install --no-install-recommends gnome-shell ubuntu-session ubuntu-settings gnome-shell-extension-ubuntu-dock \
  gnome-shell-extension-appindicator gnome-shell-extension-desktop-icons-ng \
  yaru-theme-gtk yaru-theme-icon ubuntu-wallpapers-noble fonts-ubuntu nautilus gvfs \
  xdg-user-dirs dbus-daemon x11-utils xwayland libgl1-mesa-dri \
  gnome-text-editor gnome-terminal zenity gnome-keyring python3-venv python3-keyring libnotify-bin fonts-noto-cjk ca-certificates curl gpg
```

## 打开原版应用

在 GNOME 顶栏打开“活动”，搜索 **安装官方客户端**（英文 **Install official desktop apps**），选择 ChatGPT、Claude 或两者。确认后由终端下载厂商提供的软件包，APT 列出依赖并根据需要请求 sudo。可用 Enter 取消选择。完成后搜索应用名称并启动。如果早期版本的工作区缺少终端，请从 Windows 再运行一次 `Setup.cmd`。也可以在已打开的 Ubuntu 终端中，从解压目录运行 `bash linux/setup-vendor-apps.sh`。[实际安装菜单](native-ubuntu-app-installer.png)

- **ChatGPT**。OpenAI 提供 Ubuntu 24.04 的 x64 和 ARM64 Linux 桌面预览版。安装后 GNOME 通过软件包注册的 `chatgpt.desktop` 打开原程序。工作区会为这个 GNOME 会话选择 OpenAI 文档中的原生 Wayland 参数，让窗口由 Ubuntu 桌面管理。如果已在另一个显示器会话中运行 ChatGPT，请先从应用菜单完全退出，再从工作区打开。OpenAI 将原生 Wayland 支持标为实验性。[OpenAI 的 Linux 安装和兼容性说明](https://learn.chatgpt.com/docs/linux/linux-app)
- **Claude**。Anthropic 的 Linux 客户端在应用菜单注册为 `com.anthropic.Claude.desktop`。桌面图标会打开它自己的窗口。Claude 对话与 Artifacts 的可用性取决于登录账户。[Anthropic 官方安装说明](https://support.claude.com/en/articles/10065433-install-claude-desktop)
- **Windows 版本**。在 WSL 中，缺少相应 Linux 客户端时仍提供对应的 Windows 应用入口。用户选择该入口时，启动器调起已安装的官方 Microsoft Store 版本。如果尚未安装商店版，会询问是否打开官方商品页。Windows 应用在独立的 Windows 窗口中运行。[ChatGPT 商店页](https://apps.microsoft.com/detail/9plm9xgg6vks) · [Claude 商店页](https://apps.microsoft.com/detail/9p6k58ths811)

- **项目数据**。需要在应用内读取云项目时，从 GNOME “活动”打开 **连接 InstaBox 到官方客户端**，选择应用并核对项目。此连接可列目录、读文本及查询 Git 状态。要保存文件，可用下述文件管理器操作。Claude 的聊天历史及 Artifacts、ChatGPT 的聊天历史继续受各自的账号与功能权限控制。[连接与验证方法](official-client-workspace.md)
- **在 Ubuntu 直接编辑项目文件**。完成绑定后，在“活动”打开 InstaBox 项目文件，从云端选一个文件，双击本地工作副本即可用系统编辑器修改。客户端会话和文件管理器可以独立使用。[原生文件操作](native-project-files.md)

在有图形会话的 Linux 上，可用 `python3 launcher.py --check-native` 检查前置条件，再运行 `python3 launcher.py` 启动本地会话。Ubuntu 桌面会为已安装的官方 ChatGPT 和 Claude 提供快捷方式。Windows 应用入口在 WSL 中注册。已有远端 Xfce 和浏览器项目工作台分别从 `--cloud-desktop` 与 `--workbench` 进入。

Ubuntu 24.04 的 GNOME Shell 46 启动时读取父显示的可用区域，给桌面边框和任务栏留出空间。Linux 嵌套窗口实测在 1920×1080 显示上得到 1888×1016 的桌面，在 1280×720 上得到 1248×656。这个版本的 Mutter 在运行中固定嵌套窗口大小，更换显示后可关闭再打开。Windows 的显示缩放仍需实机验证。[逐项测量](measurements/native-gnome-window-size.json)

当 WSLg 把父 X11 套接字目录挂为只读时，工作区使用 Wayland 运行 Ubuntu 内的应用。在 Linux 上重现这一挂载方式后，已核对 Nautilus、Claude 和 ChatGPT。对 ChatGPT，还在 Mutter 侧检查了窗口标题和创建它的进程。仅支持 X11 的 Linux 程序需要可用的嵌套 Xwayland。

## 在 Windows 上验证桌面窗口

完成首次准备后，在 Windows 11 的解压目录打开 PowerShell，运行以下命令。有多个 Ubuntu 时，在末尾加上 `-Distro Ubuntu-24.04` 或你注册的完整名称。

```powershell
powershell.exe -NoProfile -File .\windows\Test-NativeWorkspace.ps1
```

1. 脚本通过 `Start.cmd` 启动新桌面。Ubuntu 24.04 日常窗口的标题是 **Cloud Linux Workspace**。这次验收的窗口标题另带一串方括号标识，用来区分已打开的其他桌面。
2. 等 Ubuntu 文件管理器打开名为 **Files** 的目录，点击窗口使它位于前台。看到 Ubuntu 桌面通知后，按 **Ctrl+Shift+N**，输入通知中的 `Verified-xxxxxxxx` 名称，按 Enter。让工作区保持在前台，直到验证结束。默认操作时间为 180 秒，可用 `-TimeoutSeconds` 调整。
3. PowerShell 最后一行是 JSON。`status` 为 `verified` 且退出码为 0，表示本次运行观测到同一个 Windows 前台窗口、对应的 Ubuntu GNOME 和 Nautilus 进程，以及刚创建的空目录。报告有窗口句柄、宿主进程、Ubuntu 版本和两个 Linux 进程号，可留作该机器的验收记录。若失败，退出码为 1，查看 `phase` 和 `errorCode`。例如 `foreground-not-observed` 表示创建前后的前台窗口采样不完整。

命令使用本机临时目录，无需登录厂商账户或连接 InstaBox。验收结束后桌面继续运行，脚本清理它识别的临时目录。若向目录里添加了其他文件，`cleanup` 会显示 `preserved` 并保留内容。验收仅采样前台和文件系统结果，操作仍需由人在文件管理器内完成。[宿主验收脚本](../windows/Test-NativeWorkspace.ps1) · [Ubuntu 实测原图](native-ubuntu-files-verification.png) · [进程与文件记录](measurements/native-windows-verifier.json)

在同一台 Windows 机器验收 Linux 客户端时，从 GNOME 内分别打开 ChatGPT 和 Claude。拖动应用标题栏，把窗口移到壁纸中央，再从 Windows 任务栏最小化整个工作区。还原后应回到同一个应用窗口。此项操作可在登录前完成。登录后再检查 ChatGPT 侧栏、输入与回复，以及 Claude 的 Artifacts 面板。在已授权项目里打开一个已知文件，并在云机上独立比对内容，作为项目连接的验收。[项目连接](official-client-workspace.md)

## 官方客户端验收

- 在 Ubuntu 桌面双击 ChatGPT，打开登录页和选择登录方式的面板。最小化整个工作区时，ChatGPT 随之消失。还原后再次看到登录页，子 Mutter 记录的客户进程与 OpenAI 安装包匹配。只有回环接口时也能操作这个面板。[Ubuntu 桌面](native-ubuntu-official-apps.png) · [ChatGPT 登录页](native-ubuntu-chatgpt.png) · [离线点击后的窗口](native-ubuntu-chatgpt-offline-options.png) · [进程与窗口归属记录](measurements/native-linux-clients.json)
- 在 Ubuntu 24.04.5 的 GNOME Shell 46.0 上，使用 Dock 中的原版文件管理器新建了目录并核对了磁盘结果。随后通过“活动”启动了 Anthropic 官方签名的 Claude Desktop 6.28899.6。程序映像与签名软件包中的文件摘要相同。[Ubuntu 桌面截图](native-ubuntu-desktop.png) · [Claude 窗口截图](native-ubuntu-claude.png)
- 退出 Claude 后，双击桌面图标会重新打开客户端。登录前就可以展开原厂菜单，实测包含 File、Edit、View、Go、Help。[桌面图标打开的 Claude 菜单](native-ubuntu-claude-menu.png)
- 在同一个 Ubuntu 用户目录中连续两次启动工作区。第二次启动前挂载 Claude 官方 Debian 包的原始文件，GNOME 随即在原位置显示包里的 256 像素图标，双击启动的程序也与该包一致。这张早期截图里的 ChatGPT 尚未安装，显示的是系统通用图标。[真实桌面截图](native-ubuntu-workspace.png) · [图标、进程与运行记录](measurements/native-shortcut-icons.json)
- 在只有回环接口的 Ubuntu 环境中，用全新用户配置双击启动官方 Claude。窗口显示 `ERR_INTERNET_DISCONNECTED`，可以展开 File 菜单，看到 Settings、Close Window、Quit。完整实拍含 Linux 窗口管理器绘制的 Cloud Linux Workspace 标题栏。[离线整屏原图](native-ubuntu-branded-claude.png) · [运行中程序摘要](measurements/native-windows-verifier.json)
- 客户端保持沙盒与 TLS 校验，验收到公开欢迎页。登录后的侧栏、聊天记录及 Artifacts 仍需获授权账户验收。
- Windows 启动程序已按 x64、ARM64 构建并检查 PE 的 GUI 子系统。当前测试机是 Linux。Windows 11 上的实际 WSLg 窗口、真实 ChatGPT 窗口及端到端焦点切换尚待真机验证。

在已登录的 Windows 桌面运行以下命令。脚本核对当前用户的 Microsoft Store 应用身份，调用日常入口使用的启动程序，再检查可见窗口。

`activationPid` 是 Windows 接受激活请求时返回的进程号。`after` 记录可见窗口的进程和包身份。`observation` 区分新窗口、已有窗口移到前台和原有窗口持续可见。报告省略窗口标题和聊天内容。

```powershell
powershell.exe -NoProfile -File .\windows\Test-OfficialAppWindow.ps1 -App ChatGPT -TimeoutSeconds 60
powershell.exe -NoProfile -File .\windows\Test-OfficialAppWindow.ps1 -App Claude -TimeoutSeconds 60
```

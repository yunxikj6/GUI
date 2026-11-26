# Windows 云工作区

解压整个 ZIP，双击 `Start.cmd`。在两个 Windows 提示框中依次输入 InstaBox API 密钥和云端 GNOME 桌面的本次配对码。Windows 窗口使用显卡的 OpenGL 驱动执行云端桌面发来的绘图命令。Ubuntu、GNOME 会话和应用文件都在云机上。

客户端自带 x64 CPython 3.13.17 和 VirGL。x64 Windows 直接运行内置运行时。ARM64 启动器也使用这套 x64 运行时，需要 Windows 11 的 x64 仿真和可用的 OpenGL 驱动。ARM64 实机渲染仍待验证。

## 准备云端 GNOME

在 InstaBox 的 Ubuntu 24.04 amd64 云机解压同版本源码。桌面依赖 GNOME Shell、Ubuntu 会话、Ubuntu Dock、Yaru 主题、Ubuntu 壁纸、Mesa、VirGL、Xvfb 和 D-Bus。云端管理员可在标准 Ubuntu 源安装这些组件。

```sh
sudo apt-get update
sudo apt-get install gnome-shell ubuntu-session ubuntu-settings gnome-shell-extension-ubuntu-dock \
  yaru-theme-gnome-shell yaru-theme-gtk yaru-theme-icon ubuntu-wallpapers ubuntu-wallpapers-noble \
  xvfb xauth xdotool x11-utils dbus-daemon virgl-server libgl1-mesa-dri fonts-noto-cjk python3
python3 -m cloud_render.gnome_relay --port 17325
```

在源码根目录运行最后一条命令。服务只监听云机回环端口，Windows 客户端通过 InstaBox 私有通道连接。如果有多台云机，或管理员指定了其他服务端口，可在 Windows 终端运行下面的命令。

```bat
Start.cmd --machine m_YOUR_MACHINE --desktop-port 17325
```

本机能够连接 InstaBox SSH 网关时，也可用官方 CLI 建立持续的私有转发。先在已登录 CLI 的终端运行下面的命令，将 `dev` 换成云机名称或 ID。`41325` 是本机回环端口，`17325` 是云端 GNOME 服务的回环端口。[InstaBox CLI](https://instabox.sh/docs/cli) · [InstaBox SSH](https://instabox.sh/docs/access/ssh)

```sh
instabox port-forward dev 41325:17325 --address 127.0.0.1
```

保持转发终端运行，在 Windows 客户端目录另开终端执行以下命令。桌面客户端只提示输入 GNOME 配对码。原始绘图和键鼠回执沿同一条已认证连接持续传输。关闭桌面后可在转发终端按 Ctrl+C 释放本机端口。独立 OpenSSH 测试网关中，实际 Win64 窗口读取的 165 个画面样本均与云端原桌面匹配。当前执行器连接 InstaBox 动态 SSH 网关时收到 TCP 拒绝，供应商网关上的端到端时延仍待实测。[转发验收记录](measurements/gnome-private-forward.json)

```bat
Start.cmd --desktop-forwarded-port 41325
```

窗口支持指针移动、左右点击以及 Tab、Shift+Tab、Enter、方向键等导航键。可按 Ctrl+Esc 切换 GNOME 活动概览。默认桌面尺寸是 800×520。启动新会话时可指定 1280×900 或 1920×1080，例如运行 `Start.cmd --desktop-size 1920x1080`。Ubuntu 将按所选尺寸排布原版桌面。会话期间调整 Windows 窗口大小会同步缩放显示和点击坐标。较大尺寸需要云机也运行支持尺寸协商的版本。

## 单独打开原版 ChatGPT

在云机图形会话中安装 OpenAI 官方 ChatGPT for Linux，使用该版本源码，在云端 Python 3.11 及以上虚拟环境内启动应用服务。[OpenAI 的 Linux 客户端](https://learn.chatgpt.com/docs/linux/linux-app) · [工作区源码](https://github.com/yunxikj6/GUI/tree/workbench/reviewed-edits)

```sh
python3 -m pip install -r requirements-render.txt
python3 -m cloud_render.service --port 17324
```

在 Windows 运行 `Start.cmd --cloud-app`，按提示输入 InstaBox API 密钥和这个应用服务生成的配对码。此模式需要 Microsoft Edge，使用独立窗口和随包的 Skia 重绘原版界面。已公开实测登录前窗口的点击、英文输入、编辑、中文输入法提交与取消。登录后的聊天、附件和 Claude Artifacts 仍待验收。

已有本机端口转发时，运行 `Start.cmd --connect-port 17324`。此处指定本机回环端口，仅需应用配对码。`CLOUDWORKSPACE_RENDER_PORT` 可配置 `--cloud-app` 默认使用的本机转发端口。若要连接指定云机上的其他应用端口，使用下面的命令。

```bat
Start.cmd --cloud-app --instabox --machine m_YOUR_MACHINE --connect-port 17324
```

应用中的 Tab、Shift+Tab 可切换焦点，Enter 激活控件。可编辑控件支持方向键、Home、End、退格、删除和 Ctrl+A。Ctrl+V 粘贴本机剪贴板的纯文本。输入法候选在本机选择，确认后发送。每次提交最多 4096 个 UTF-8 字节。云端确认当前操作后，客户端才会发送下一项。断开连接后可缩放已收到的应用画面。如果窗口提示操作结果未确认，重连后先查看原窗口，再决定是否重复操作。

打开 GNOME 概览后，点击搜索框可使用 Windows 输入法。组合中的文字留在本机，提交后由云端原控件处理。连接断开后窗口转为只读，可以继续缩放。在指令缓存限额内，客户端还会用新建的本地绘图上下文重新执行最后一次成功显示前的指令。[输入法实拍](gnome-search-ime.md) · [断开后的本机绘制](gnome-source.md#断开后的本机绘制)

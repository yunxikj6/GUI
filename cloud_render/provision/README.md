# 从云服务器启动一次性 Ubuntu 桌面

在 **Debian 13 amd64 云机** 上，以普通用户进入解压的 `cloud-workspace-server` 目录或本仓库根目录。需要连接基础桌面客户端时，运行下列命令。云机需要 Python 3.12 或更高版本、APT、dpkg-deb、可用的用户和挂载命名空间，以及至少 1.5 GiB 临时空间。Ubuntu 包的下载和解包均在这台云机完成。

```sh
python3 -m cloud_render.provision
```

终端会显示本次 **云端桌面配对码**。等待出现“云桌面私有绘图端口”后，在自己的 Windows 电脑打开客户端 ZIP 中的 Start.cmd，输入配对码和 InstaBox API 密钥。默认端口为 17325。若要用其他回环端口，在云端指定 `--port 17400`，本地用 `Start.cmd --desktop-port 17400`。

引导程序在新的 0700 临时目录中创建 APT 状态，从 Canonical 的 Noble、Noble Updates 和 Noble Security 索引验签、解析依赖，并逐个校验下载包的 SHA-256。它将原 Ubuntu 文件解到独立目录，在隔离命名空间中生成桌面缓存，以只读系统目录启动私有 GNOME 服务。配对码从管道进入服务。自动化控制器可通过私有进程环境 `CLOUDWORKSPACE_GNOME_TOKEN` 提供已有的 43 字符配对码。

保持云端命令运行。按 Ctrl+C 或结束控制进程后，独立守护进程会终止本次工作进程组并回收该目录。默认生命周期为四小时，包括下载时间。可用 `--lifetime-minutes` 设定 1 至 1440 分钟。每次新启动都会重新核对软件源并创建新配对会话。

[Ubuntu Archive](https://archive.ubuntu.com/ubuntu/) · [Ubuntu Security](https://security.ubuntu.com/ubuntu/) · [随附公开 APT 密钥的来源与许可证](keys/COPYRIGHT)

一次 InstaBox 冷启动用时 404.125 秒，从 Canonical 签名索引解析并解包 495 个软件包。原版 GNOME 收到 Super 键，测试端读取了 12 个经校验的指令块。退出后独立检查确认临时环境已回收。[冷启动测量](../../docs/measurements/instabox-ubuntu-bootstrap.json)

## 同一桌面内打开原版 ChatGPT

原厂应用桌面的客户端使用端口 17326。Debian 云服务器上准备 OpenAI 的 ChatGPT for Linux amd64 包。在云机可读路径保存文件，然后运行以下命令。

```sh
python3 -m cloud_render.provision --chatgpt-deb /path/to/chatgpt_amd64.deb
```

当前适配版本为 26.1120.50957。云机在下载 Ubuntu 前先把软件包复制到本次会话的私有目录，校验与厂商签名索引匹配的 SHA-256。它在临时 Ubuntu 中安装原版软件、输入与字体依赖和绘图观测器，由云端 GCC 编译 Mutter 图面来源的记录组件。四个云端 Python wheel 的文件、尺寸及摘要固定在随包清单中。启动前的空间检查包含原软件 1,560,353 KiB 的安装大小。[原版软件来源](https://learn.chatgpt.com/docs/linux/linux-app)

看到本次私有端口后，在 Windows 解压**原厂应用桌面客户端**再执行其中的 `Start.cmd`，通过相同配对码连接。这个版本支持指定软件的登录前窗口；专用原程序使用一次性配置，图形进程的隔离网络未启用外网登录。用户输入与项目数据的持久会话，以及登录后的官方界面仍需单独接入。预装了原版软件和依赖的 Ubuntu 24.04 云机也可从仓库运行 `python3 -m experiments.original_desktop_source`。[云端条件与操作实测](../../docs/interactive-original-desktop.md)

在预装 Ubuntu 入口的私有终端，配对提示可直接按回车，让云机生成本次随机配对码。

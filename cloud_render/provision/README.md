# 从云服务器启动一次性 Ubuntu 桌面

在已检出本仓库的 **Debian 13 amd64 云机** 上以普通用户运行下列命令。云机需要 Python 3.12 或更高版本、APT、dpkg-deb、可用的用户和挂载命名空间，以及至少 1.5 GiB 临时空间。Ubuntu 包的下载和解包均在这台云机完成。

```sh
python3 -m cloud_render.provision
```

终端会显示本次 **云端桌面配对码**。等待出现“云桌面私有绘图端口”后，在自己的 Windows 电脑打开客户端 ZIP 中的 Start.cmd，输入配对码和 InstaBox API 密钥。默认端口为 17325。若要用其他回环端口，在云端指定 `--port 17400`，本地用 `Start.cmd --desktop-port 17400`。

引导程序在新的 0700 临时目录中创建 APT 状态，从 Canonical 的 Noble、Noble Updates 和 Noble Security 索引验签、解析依赖，并逐个校验下载包的 SHA-256。它将原 Ubuntu 文件解到独立目录，在隔离命名空间中生成桌面缓存，以只读系统目录启动私有 GNOME 服务。配对码从管道进入服务。自动化控制器可通过私有进程环境 `CLOUDWORKSPACE_GNOME_TOKEN` 提供已有的 43 字符配对码。

保持云端命令运行。按 Ctrl+C 或结束控制进程后，独立守护进程会终止本次工作进程组并回收该目录。默认生命周期为四小时，包括下载时间。可用 `--lifetime-minutes` 设定 1 至 1440 分钟。每次新启动都会重新核对软件源并创建新配对会话。

[Ubuntu Archive](https://archive.ubuntu.com/ubuntu/) · [Ubuntu Security](https://security.ubuntu.com/ubuntu/) · [随附公开 APT 密钥的来源与许可证](keys/COPYRIGHT)

一次 InstaBox 冷启动用时 404.125 秒，从 Canonical 签名索引解析并解包 495 个软件包。原版 GNOME 收到 Super 键，测试端读取了 12 个经校验的指令块。退出后独立检查确认临时环境已回收。[冷启动测量](../../docs/measurements/instabox-ubuntu-bootstrap.json)

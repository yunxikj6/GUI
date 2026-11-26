# 在云端采集原版 ChatGPT 窗口的绘图与来源

这个独立实验运行在拥有应用的 Ubuntu 24.04 x86-64 云机。Ubuntu Dock 从厂商的 `chatgpt.desktop` 启动已安装的 ChatGPT 26.1120.50957，程序在全新配置里打开原版登录页。实验先采集欢迎页，再点击原版的 “Sign in another way”。两个状态都不需要账户。运行脚本创建独立的进程、网络、X、Wayland 和 D-Bus 会话。

## 源机条件

需要 Noble 的 GNOME Shell 46、Ubuntu 会话、Ubuntu Dock、Yaru、发行版壁纸、Mesa virpipe、`virgl_test_server`、`gnome-keyring`、`bubblewrap`、`Xvfb`、`xauth`、`xdotool`、`xwininfo`、`gdbus`、GCC 与 OpenSSL 开发头文件。Python 3.11 及以上环境应包含 Playwright 与支持 XCB 的 Pillow。Playwright 通过私有管道启动云机已经安装的程序。专用源机须支持无特权的嵌套命名空间。

源码使用系统默认的 `/usr/bin/chatgpt`、`/usr/share/applications/chatgpt.desktop` 和 `/usr/lib/chatgpt/ChatGPT`。启动时核对 ELF 的 SHA-256 为 `b36d94aff6ec46074f9d4ac5f898d1cc449396b661663487fcf2d11e41c502a7`。新版本需重新测量绘图、Wayland 与 Mesa 的行为。

源码默认在源机编译观察器。空间较小的云端也可使用操作者在兼容的 x86-64 构建环境中预编译的观察器。把同一份 `mutter_witness.c`、编译出的 `witness.so` 和两个文件的 `sha256sum` 清单 `SHA256SUMS` 放在可信目录，设置 `CW_MUTTER_PREBUILD` 指向它。脚本核对源码与文件摘要，源端此时只需 OpenSSL 3 运行库。

在源机检出仓库，用已激活的 Python 环境运行以下命令。为输出选一个尚不存在的私有目录，并预留 512 MiB 命令归档空间以及临时原厂缓存空间。

```bash
bash experiments/mutter_app/run.sh "$HOME/chatgpt-render-proof-01"
```

实验使用官方 GNOME 的 1280 × 900 虚拟显示。真实指针在最小化、关闭按钮和正文间移动一次，使原生标题栏产生可采集的绘图，再回到正文。轻量客户端无需安装实验的 Ubuntu、Mutter、Chromium 或厂商程序。

## 产物与边界

- `before.clrp` 和 `after.clrp` 包含同一原版窗口的正文 SkPicture、原生按钮容器及两个原生按钮的 SkPicture。目前验证的窗口为 1090 × 760、DPR 1、浅色、标题栏按钮静止。三个按钮层记录的 SHA-256 随源码提供，实际绘图每次从源程序取得。悬停、缩放、额外窗口或未知样式均中止采集。
- `capture.json` 记录专用 CDP 管道对应进程的 PID、内核启动时钟、可执行设备与 inode、两份绘图摘要及源端观察区间。源端对官方页面参考像素作摘要，用于检验两次观察间业务区域的稳定性。驱动用来定位真实窗口的临时参考图在指针移动前删除。
- `owner.log` 记录专用 Mutter 进程观察到的已安装 ELF 的 Wayland 客户端、xdg 角色、缓冲、真实提交、Cogl/GL 资源及私有 socket 的绝对写入偏移。加载器的 `--preload` 仅施加于这个开源合成器。应用和浏览器子进程使用厂商原件。软件路径限定为 wl_shm、GL_TEXTURE_2D 和 Mesa vtest 0。强制的 GPU 同步会改变测量时延。
- `commands.virgl` 是**仅限源机**的验证输入，含原合成器上传的窗口像素。`provenance.json` 将每个已观察的应用 PUT 绑定至真实提交、资源创建代次和字节偏移。只有真实 SHM、原厂窗口、标题栏和同一时段原始绘图均通过验证的记录才有 `pictureSha256`。客户端应等待没有绘图配对的提交。客户端的原始资源替换实验见 [应用表面重绘](../../docs/application-surface.md)。
- 运行未完成时可在源机检查 `shell.log`、`broker.log`、`input.log`、`relay.log`。代码可直接重验同一份源采集。不会把原始私有归档、日志、账户资料或厂商二进制加入项目。

```bash
python -m experiments.mutter_app.bind "$HOME/chatgpt-render-proof-01"
python -m unittest tests.test_mutter_app_source -v
```

此实验使用云源角色验证两种登录前画面的绘图和资源来源。真云跨机、不同分辨率、其他应用以及 Windows 11 实机应分别记录实测证据。

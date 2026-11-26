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

## 同一活跃会话的交互实验

源机可同时创建一对仅本用户可打开的 FIFO。每次运行使用全新的源输出目录、配对目录和会话随机数。命令流从原版 GNOME 启动时开始发送。远端须先打开 `graphics.fifo` 读端，并在独立线程打开 `input.fifo` 写端。Windows 顺序验证独立的 `session.json`、随后出现的 `binding.json` 与流内绑定。配对目录只包含这两份身份数据和两条管道。

```bash
bash experiments/mutter_app/run.sh --live "$HOME/live-pair-01" "$HOME/live-source-01"
```

源端在专用 Ubuntu 会话里关闭 Desktop Icons NG 这个附加客户端。它会产生独立的整桌图像表面。原版 Shell、Ubuntu Dock、官方壁纸与已安装的 ChatGPT 参加此次单应用实验。云源观察器对每个原始 Mesa PUT 提供字节偏移与实际同步绘图范围。接收路径会等待缺失的观察，并拒绝未经认定的其他应用 SHM。所有原应用内容纹理在源端替换为原厂绘图或等待记录。数据暂存在有界内存队列，当前会话不写本机原始图像归档。

Windows 窗口接收并绘制登录页后，操作者可点击 “Sign in another way”。配对写端发送字节 `L` 加两个大端 16 位整数，坐标以 1280 × 900 的云桌面为基准。该点击在同一私有 Xvfb 上操作原版窗口。程序等待原版菜单出现，并采集、校验新绘图。`input-applied.json`、`forwarded-before.json`、`forwarded-after.json` 和 `live-source.json` 在源机记录输入、实际提交和已分类的流量。此入口限一次点击及两个已验证的未登录状态。

## 手动桌面入口

```bash
bash experiments/mutter_app/run.sh --interactive "$HOME/manual-pair-01" "$HOME/manual-source-01"
```

通过同一受认证的私有转发连接两条 FIFO。Windows 用户打开查看器后，先看到原版 Ubuntu 桌面；点击左侧 Dock 的 ChatGPT 图标，再从原版欢迎页选择登录方式。手动模式在原厂窗口正常启动时观察浏览器绘制三个原生标题栏图层，无需用户移动鼠标唤起采集。启动与菜单点击都来自用户。云源持续核对原窗口的登录前状态，对实际改变的合成提交重新采集官方绘图。当前支持浅色、DPR 1、1090 × 760 的欢迎页与登录方式菜单。

输入记录为 `M` 或 `L` 加两个大端 16 位桌面坐标。云端实际投递移动或左键后，在配对目录原子更新 `last-input.json`，含 `sequence` 和 `applied`。控制器关闭输入流后，源端结束本次专用会话。手动入口无需 OCR。检测合适点击位置的 XTest/OCR 在独立验收器中运行。

在一次独立无网 Ubuntu 源与 Wine 下真实 Win64 窗口的双向实验中，源端观测到同一原程序的 Wayland surface 33、VirGL 资源 203。实际窗口接收一次左键 (672, 502) 后，原厂欢迎页和登录方式菜单分别通过核验。源端从 138,919,700 字节原始 Mesa 流中移除了 97,838,400 字节应用纹理。过滤流为 44,898,735 字节，携带两份原始绘图。56 个缺少配对绘图的提交进入等待，两个状态分别通过 5 和 7 个提交。该次运行是双向受控验收；本实验的 Windows 11 实机与 InstaBox 真云验证分别记录。

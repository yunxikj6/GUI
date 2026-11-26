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

源机可同时创建一对仅本用户可打开的 FIFO。每次运行使用全新的源输出目录、配对目录和会话随机数。命令流从原版 GNOME 启动时开始发送。远端须先打开 `graphics.fifo` 读端，并在独立线程打开 `input.fifo` 写端。Windows 顺序验证独立的 `session.json`、随后出现的 `binding.json` 与流内绑定。手动模式另外提供删除、文字和焦点的受认证输入回执。

```bash
bash experiments/mutter_app/run.sh --live "$HOME/live-pair-01" "$HOME/live-source-01"
```

源端在专用 Ubuntu 会话里关闭 Desktop Icons NG 这个附加客户端。它会产生独立的整桌图像表面。原版 Shell、Ubuntu Dock、官方壁纸与已安装的 ChatGPT 参加此次单应用实验。云源观察器对每个原始 Mesa PUT 提供字节偏移与实际同步绘图范围。接收路径会等待缺失的观察，并拒绝未经认定的其他应用 SHM。所有原应用内容纹理在源端替换为原厂绘图或等待记录。数据暂存在有界内存队列，当前会话不写本机原始图像归档。

Windows 窗口接收并绘制登录页后，操作者可点击 “Sign in another way”。配对写端发送字节 `L` 加两个大端 16 位整数，坐标以 1280 × 900 的云桌面为基准。该点击在同一私有 Xvfb 上操作原版窗口。程序等待原版菜单出现，并采集、校验新绘图。`input-applied.json`、`forwarded-before.json`、`forwarded-after.json` 和 `live-source.json` 在源机记录输入、实际提交和已分类的流量。此入口限一次点击及两个已验证的未登录状态。

## 手动桌面入口

```bash
bash experiments/mutter_app/run.sh --interactive "$HOME/manual-pair-01" "$HOME/manual-source-01"
```

通过同一受认证的私有转发连接两条 FIFO。Windows 用户打开查看器后，先看到原版 Ubuntu 桌面；点击左侧 Dock 的 ChatGPT 图标，再从原版欢迎页选择登录方式。手动模式在原厂窗口正常启动时观察浏览器绘制三个原生标题栏图层，无需用户移动鼠标唤起采集。启动与菜单点击都来自用户。云源持续核对原窗口的登录前状态，对实际改变的合成提交重新采集官方绘图。支持范围为同一原厂进程、原始页面、1090 × 760、DPR 1 和经验证的浅色标题栏。窗口内容改变时，程序按真实 Wayland 提交取得新的修订号，重新采集原厂 SkPicture。相同业务区域可复用同一窗口的原始录制。云源保留最多 16 份、合计 48 MiB 的绘图，缺失原始绘图的提交继续等待。

指针采用 `M/L` 加两个大端 16 位桌面坐标，导航键采用 GNOME 协议的 `K`。原厂可编辑字段获得焦点时，源端追加一个 5 字节的焦点事件到私有 `focus.events`。每个事件包含大端 32 位代次和一字节活动状态。协议的 `T`（UTF-8 文本）、`A`（全选）、`B/D`（向前或向后删除）必须携带当前代次，且接收时仍须是同一个原厂编辑控件、值和选区。云端执行或拒绝操作后原子更新 `last-input.json`，仅含 `sequence` 和 `applied`。同一配对目录的 `editor-capabilities.json` 绑定本次会话标识与支持的能力位。源端拒绝不带代次的删除操作。控制器关闭输入流后，源端结束本次专用会话。手动入口无需 OCR。检测合适点击位置的 XTest/OCR 在独立验收器中运行。

在一次独立无网 Ubuntu 源与 Wine 下真实 Win64 窗口的双向实验中，源端观测到同一原程序的 Wayland surface 33、VirGL 资源 203。实际窗口接收一次左键 (672, 502) 后，原厂欢迎页和登录方式菜单分别通过核验。源端从 138,919,700 字节原始 Mesa 流中移除了 97,838,400 字节应用纹理。过滤流为 44,898,735 字节，携带两份原始绘图。56 个缺少配对绘图的提交进入等待，两个状态分别通过 5 和 7 个提交。该次运行是双向受控验收；本实验的 Windows 11 实机与 InstaBox 真云验证分别记录。


### 签出表单的实测范围

一次独立 Ubuntu 云源角色测试使用禁网的临时配置，从真实 Dock 打开欢迎页、登录菜单和原厂 OpenAI API Key 表单。在表单中仅输入无效占位文本，随后退格、全选、替换、清空，并用原版 Cancel 和 Back 返回欢迎页。测试涵盖十个实际修订，源端认证了 502 次应用纹理更新，129 次更新因缺少配对绘图而进入等待。过期焦点的输入返回拒绝。恢复空白表单和返回登录菜单时，分别再次使用此前验证的原厂绘图。此轮的真实源侧截图和独立 CanvasKit 回放之间，整个 1090 × 760 原应用区域的逐通道平均绝对差低于 0.25（0–255）。测试没有提交表单或连接厂商服务。Windows 窗口和真实 InstaBox 网络链路分别验收。


同一真实编辑框内的连续输入沿用焦点代次。原程序只有在一次渲染器任务内完成本次操作、焦点和节点仍一致、值及选区恰好达到预期时，才会更新该代次对应的快照。外部光标移动、重新聚焦或外部改值会使旧代次失效。独立源端测试连续发送七个单字符提交，同一原版输入框显示完整的 `invalid`；实际向左移动光标后，原代次的文字和删除操作均被拒绝。


原窗口的实际 Wayland 缓冲提交会唤醒云端采集器。源端先核对应用身份、图形资源和缓冲，再为没有匹配绘图的状态取得原厂参考像素和 SkPicture。已认证的静止窗口不触发应用截图。相同原包、同一尺寸的独立对照中，旧轮询器在静止欢迎页 8.38 秒调用 12 次参考截图，产生 24 个额外应用提交。新方式的 8.00 秒内两项均为零；随后从真实 Dock 会话继续完成十个窗口修订，包括未提交表单的七次连续字符输入、删除、全选、取消及返回。


源端读取参考画面时显式保留应用自身的插入光标样式。测试中的真实 Mutter 在光标切换时记录到 1 × 15 像素的原生损伤提交；带这一设置的独立窗口完成了文本编辑、选择与返回。选择区、文本与按钮仍来自同一原厂绘图。各次闪烁是否都能在 Windows 上及时重绘需另行测量。

### 在云源核对一次物理指针投递

在已安装本实验依赖的 Ubuntu Noble 云源上，可以为一次专用测试会话启用只读诊断。它读取 Mutter 发送给原厂 Wayland 客户端的 enter、motion、leave、button，以及当前 MetaWindow 和 Clutter actor 的实际位置。普通构建省略这段诊断。坐标写入源机的私有目录，完成诊断后由操作者清理。

```bash
build=$(mktemp -d)
install -m 600 experiments/mutter_app/mutter_witness.c "$build/mutter_witness.c"
gcc -std=gnu11 -fPIC -shared -Wall -Werror -O2 -DCW_POINTER_DIAGNOSTICS \
    -o "$build/witness.so" "$build/mutter_witness.c" -ldl -lcrypto -pthread
(cd "$build" && sha256sum mutter_witness.c witness.so > SHA256SUMS)
CW_MUTTER_PREBUILD="$build" experiments/mutter_app/run.sh --interactive "$HOME/ptr-pair-new" "$HOME/ptr-source-new"
python3 -m experiments.mutter_app.pointer_diagnostics "$HOME/ptr-source-new"
```

按上面的手动模式配对 Windows 窗口并操作。关闭窗口后，最后一条命令核对原厂进程、Wayland 连接、主窗口、已完成的绘制记录以及 Mutter 的三份几何数据。报告只列出原窗口内按下并松开的左键。移动时的 Wayland 坐标保留 1/256 像素精度。在独立的 1280 × 900 Ubuntu 会话里，窗口原点为 (127, 67)，桌面 (672, 502) 的物理点击到达应用内 (545, 435)，随后出现厂商登录方式菜单。坐标反映当次窗口位置。本工具只能证明系统把输入投递到哪个窗口。应用内部控件的身份、下一次点击的许可与绘图签发均由各自原有路径处理。

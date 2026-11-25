# GTK Broadway 实验记录

**GTK 窗口可以在浏览器中显示并接收按键。在本次实验的版本和控件组合下，选择范围保存在 GTK 进程中。** 这意味着平台可以沿这条路线接入现成工具包的程序；若要支持断网时继续编辑、浏览器原生查找和选择等功能，还需要同步相应的应用状态。

## 测了什么

2026 年 11 月 25 日在 Debian 13.7 x86-64 开发机执行。三个互相独立的进程是 GTK 原生程序、绑定到 `127.0.0.1` 随机端口的 `gtk4-broadwayd`、Playwright 启动的无头 Chromium。GTK 4.18.6，Chromium 156.0.8078.79，Playwright Python 1.62.0。显示区域为 800 × 520。实验未连接 InstaBox。每次都创建新的浏览器页面、GTK 程序和运行目录。

GTK 程序创建一个 480 × 180 窗口，含可选中的中英文 `GtkLabel` 和 `GtkEntry`。脚本检查浏览器 DOM、WebSocket 帧和截图，同时独立读取原生 GTK 控件的 `changed`、`activate` 事件。GTK 官方说明 Broadway 使用 HTML5 和 WebSocket，后端对 GTK 新特性的覆盖存在版本差异。[GTK Broadway 文档](https://docs.gtk.org/gtk4/broadway.html)

| 动作 | 三次独立运行的观测 |
| --- | --- |
| 打开窗口，枚举正文 DOM | 正文有 38–39 个 `DIV`、8 个 `IMG`。正文 `textContent` 搜不到本例 GTK 标签文字。页面中 `input` 和 `textarea` 数量均为零。 |
| 从浏览器逐键键入 `Probe42` | GTK 的输入变化日志依次收到七个前缀直到完整文本。前后截图均发生变化。 |
| 输入框中按 Ctrl+A，读取浏览器选区，然后按回车 | 浏览器的 `window.getSelection().toString()` 均为空。原生 GTK 返回选中区间 `[0, 7]` 和完整文本 `Probe42`。 |
| 暂停 GTK 进程，保持浏览器与 Broadway 服务运行，再按 End 并输入 `Z` | 暂停期间 350 毫秒内 GTK 事件数保持不变。恢复进程后收到了 `Probe42Z`。此项只检测控件状态，未测冻结期间的光标视觉反馈。 |
| 调用 Chromium 的文本注入发送“中文” | 500 毫秒观察窗口内均未产生 GTK 文本变化。该操作使用 Playwright `keyboard.insert_text`，没有真实桌面输入法及候选窗参与；实际拼音选词、提交和撤回仍需人工或系统级 IME 测试。 |

浏览器建立了路径为 `/socket` 的 WebSocket。三轮收到和发出的数据帧均为二进制。以下为该页面从连接到测试末尾的有效载荷统计，包含协议控制消息及应用更新。计数未包含 WebSocket 包头、TCP/TLS 和初次加载的 HTTP 资源，也没有分离进程调度、动画或各类消息所占的比例。

| 运行 | 服务端到浏览器，帧 / 字节 | 浏览器到服务端，帧 / 字节 |
| --- | ---: | ---: |
| 1 | 2,313 / 57,150 | 2,338 / 46,788 |
| 2 | 3,068 / 66,283 | 3,093 / 61,888 |
| 3 | 2,289 / 60,405 | 2,314 / 46,308 |

## 复现

从仓库根目录运行。实验脚本独立于产品，开发机需要 Linux x86-64、可运行 GTK 4 的 PyGObject、Chromium 和 Playwright。安装了 Debian 系统 GTK 4 与 Broadway 的环境可直接运行第一条命令。脚本默认用 `/usr/bin/python3` 运行 GTK、`/usr/bin/chromium` 运行浏览器，可通过 `GTK_PYTHON` 和 `CHROMIUM` 改写。标准输出是 JSON，包含原生控件事件序列、DOM 检查及网络计数。

```sh
python experiments/broadway_probe.py
```

本次开发机已有 Chromium、PyGObject 和 Playwright。GTK 4 及缺少的动态库从 Debian 软件池取出后解压到实验目录，未安装到操作系统。下列命令使用相同的包版本。环境多次复现时可以重复使用同一个依赖目录。[GTK Debian 软件池](https://deb.debian.org/debian/pool/main/g/gtk4/)

```sh
repo_dir=$PWD
experiment_dir=/workspace/work/research
mkdir -p "$experiment_dir/packages" "$experiment_dir/tree"
cd "$experiment_dir"
while read -r archive; do
  file=${archive##*/}
  curl -fsSL "https://deb.debian.org/debian/pool/main/$archive" -o "packages/$file"
  dpkg-deb -x "packages/$file" tree
done <<'PACKAGES'
g/gtk4/libgtk-4-1_4.18.6+ds-2_amd64.deb
g/gtk4/libgtk-4-bin_4.18.6+ds-2_amd64.deb
g/gtk4/libgtk-4-common_4.18.6+ds-2_all.deb
g/gtk4/gir1.2-gtk-4.0_4.18.6+ds-2_amd64.deb
g/graphene/libgraphene-1.0-0_1.10.8-5_amd64.deb
g/graphene/gir1.2-graphene-1.0_1.10.8-5_amd64.deb
c/cairo/libcairo-script-interpreter2_1.18.4-1+b1_amd64.deb
l/lzo2/liblzo2-2_2.10-3+b1_amd64.deb
PACKAGES
export BROADWAY_PREFIX="$PWD/tree/usr" PROBE_WORK="$PWD"
export GSETTINGS_SCHEMA_DIR="$BROADWAY_PREFIX/share/glib-2.0/schemas"
glib-compile-schemas "$GSETTINGS_SCHEMA_DIR"
for run in 1 2 3; do
  python "$repo_dir/experiments/broadway_probe.py" > "broadway-$run.json"
done
```

## 工程决策

对代码差异、文件预览和代理输入框，保留本地文本模型。它给浏览器提供选区、复制和中文输入法所需的信息。对既有 GTK 应用，可以用此脚本筛查目标控件是否正确显示和接收输入，然后测试文件对话框、弹出菜单、多窗口焦点和第三方自绘控件。决定是否替代通用桌面通道，还需要在同一云机和客户端上与 VNC 做有延迟、丢包和显示缩放的对照；这里的回环计数不能用于推导公网带宽节省或往返时延。

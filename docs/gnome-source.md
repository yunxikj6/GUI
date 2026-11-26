# 原版 Ubuntu 桌面的云端绘图源

GNOME Shell 46、Mutter、Ubuntu Dock、Yaru 与 Noble 壁纸运行在云机。源端把这个 GNOME Shell 进程发出的 Mesa 绘图命令和绘图资源交给私有中继。客户端用自己的渲染器绘制本地窗口。原桌面的 GPU 回读留在云机，独立事件管道只返回完整显示帧的序号与 SHA-256。

## 云端环境与启动

在云端的 Ubuntu 24.04 amd64 普通用户环境安装发行版组件。需要 Python 3、`ubuntu-session`、`gnome-shell`、`gnome-shell-extension-ubuntu-dock`、`yaru-theme-gnome-shell`、`yaru-theme-icon`、`ubuntu-wallpapers-noble`、`xvfb`、`x11-utils`、`xdotool`、`dbus-daemon`、`libglib2.0-bin`、`libgl1-mesa-dri` 和提供 `virgl_test_server` 的 `virgl-server`。使用有完整依赖的专用虚拟机或容器，保留系统 GLib 的 `gio-launch-desktop` 程序。源端需要可写的私有临时目录、独立的进程及套接字环境。新启动的源使用独立的 HOME、桌面目录、Xauthority 和两条 D-Bus 总线。

用户通过配对中继打开云桌面。在仓库根目录运行以下命令启动云机回环服务。交互式终端会显示配对码。在服务管理器内运行时，用其私密环境配置设置 `CLOUDWORKSPACE_GNOME_TOKEN`。

```sh
python3 -m cloud_render.gnome_relay --port 17325
```

源进程默认启动发行版的 `ubuntu` 模式，等待 Shell 完成启动、Ubuntu Dock 报告已启用，并收到启动清屏后的真实桌面画面。仅诊断普通 GNOME 运行环境时才显式选用 `--session-mode gnome`。

## 进程接口

中继启动子进程 `python3 -m cloud_render.gnome_source --event-fd N`，继承一个编号至少为 3 的独立可写文件描述符，并将 stdin、stdout 接至管道。三条通道均属于此次专用会话。

| 通道 | 内容 |
| --- | --- |
| stdout | 由首个上下文建立请求开始的完整 Mesa vtest 0 命令、着色器及原始纹理资源。 |
| stdin | 每个动作一条定长记录。首字节 `S` 对应 Super，`M`、`L`、`R` 后跟网络字节序的两个无符号 16 位桌面坐标，分别表示移动、左击和右击。`K` 后跟一个导航键编号。 |
| 事件 FD | 一行一个 JSON 对象。`frame` 提供从 1 开始的 `sequence` 和完整 BGRX 显示面的 `sha256`。`ready` 提供 `width`、`height`、`frames`。`input` 提供成功交给原窗口的输入 `sequence` 以及注入前最后一个绘制序号 `framesBefore`。`unavailable` 提供 `application`，取值为 `chatgpt`、`claude`、`wayland`。 |

当前默认画面 800×520。输入来源调用 `cloud_render.gnome_input.encode_pointer` 与 `encode_key` 进行边界和编号校验。导航键包括 Tab、Shift+Tab、Enter、Escape、四向键、Backspace、Delete、Home、End、PageUp、PageDown 和 Space。每次投递前后，源端都会检查私有 X 显示器上真实 GNOME Shell 窗口的身份与焦点。

云端绘制与中继发送使用独立线程，队列按原顺序保存命令及提交边界。每次会话的排队数据最多 96 MiB，达到上限后由接收速度控制云端后续绘制。源端通过内核提供的对端 PID 和可执行文件身份核对专属 Shell，并在发布前封住外部 Wayland 连接入口。原版 Shell 可直接绘制后台、顶栏、Dock、概览和发行版自带的桌面组件。系统安装的应用启动项保留原有名称、翻译、图标和动作，普通应用的激活请求交回中继。应用自己的绘图适配尚未接合时，调用方收到 `unavailable`。ChatGPT 的独立原版绘图入口可以显示当前支持的已签出流程。[原版应用绘图说明](thin-client.md)

关闭控制管道会结束专有会话，释放该会话的进程及临时状态。新连接从新桌面和完整绘图上下文开始。

```sh
python3 -m unittest discover -s tests -p 'test_gnome_*.py'
```

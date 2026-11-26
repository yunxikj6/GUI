# 云端应用轻量客户端

本机需要 Windows x64 或 ARM64、Python 3.10 及以上版本和 Microsoft Edge。先解压整个 ZIP，再双击 `Start.cmd`。原版 ChatGPT 由云服务器运行。本机使用随包的 Skia 引擎，在独立窗口中绘制从云端收到的界面。

## 云端准备

在云机的图形会话中安装 OpenAI 官方 ChatGPT for Linux，并解压本项目完整源码。在云机的 Python 3.11 及以上虚拟环境中，从源码目录安装绘图采集依赖并启动服务。[OpenAI 的 Linux 客户端](https://learn.chatgpt.com/docs/linux/linux-app) · [工作区源码](https://github.com/yunxikj6/GUI/tree/workbench/reviewed-edits)

请让云端源码和 Windows ZIP 使用同一次发布。早期云端服务只识别点击消息。

```sh
python3 -m pip install -r requirements-render.txt
python3 -m cloud_render.service --port 17324
```

终端会显示本次私有配对码。所选端口只监听云机回环地址。应用使用云机上专属于工作区的配置目录，同一用户重连时可以继续操作同一个进程。`--profile` 可指定新的空目录。`--executable` 可指定从官方包解出的程序绝对路径。

## Windows 连接

双击 `Start.cmd` 时使用 InstaBox 私有连接，默认云端端口为 17324。按提示分别输入 InstaBox API 密钥和当前云端应用的配对码。若账户下有多台云机，请从终端启动，指定实例的完整 ID。

```bat
Start.cmd --instabox --machine m_YOUR_MACHINE --connect-port 17324
```

InstaBox 连接会在本机内存中保留本次会话刚收到的完整绘图。窗口更新时，云端可以引用其中相同的字形和图面数据。客户端核对完整帧的大小和 SHA-256 后才交给 Skia。重连会重新传输所需数据，退出时释放这份缓存。

已有本机端口转发时，可以运行 `Start.cmd --connect-port 17324`。此时端口号指本机回环端口，只需云端绘图配对码。若希望双击也使用此方式，可设置 `CLOUDWORKSPACE_RENDER_PORT`。

## 窗口操作

单击云端控件，或用 Tab、Shift+Tab 切换焦点，按 Enter 激活。可编辑控件支持文字、方向键、Home、End、退格、删除和 Ctrl+A。Ctrl+V 会将本地剪贴板的纯文本送入云端控件。每次提交最多 4096 个 UTF-8 字节。输入法的候选在本机选择，确认时才发送。云端确认每次操作后，客户端才会发送队列中的下一项。

断开云端后仍可在本机缩放已收到的画面。如果窗口提示操作结果未确认，重新连接后请先查看原窗口，再决定是否重复操作。目前公开实测范围为原版 ChatGPT 的登录前窗口。登录后的聊天和附件、Claude Artifacts、完整 GNOME 桌面与 Windows 11 真机仍待验收。

# 云端应用轻量客户端

本机需要 Windows x64 或 ARM64、Python 3.10 及以上版本和 Microsoft Edge。先解压整个 ZIP，再双击 `Start.cmd`。原版 ChatGPT 由云服务器运行。本机使用随包的 Skia 引擎，在独立窗口中绘制从云端收到的界面。

## 云端准备

在云机的图形会话中安装 OpenAI 官方 ChatGPT for Linux，并解压本项目完整源码。在云机的 Python 3.11 及以上虚拟环境中，从源码目录安装绘图采集依赖并启动服务。[OpenAI 的 Linux 客户端](https://learn.chatgpt.com/docs/linux/linux-app) · [工作区源码](https://github.com/yunxikj6/GUI/tree/workbench/reviewed-edits)

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

已有本机端口转发时，可以运行 `Start.cmd --connect-port 17324`。此时端口号指本机回环端口，只需云端绘图配对码。若希望双击也使用此方式，可设置 `CLOUDWORKSPACE_RENDER_PORT`。

目前支持鼠标左键点击、接收云端窗口变化、按本机窗口大小重新绘制。断开云端时仍可缩放已收到的画面。登录后的输入和对话、Claude Artifacts、完整 GNOME 桌面与 Windows 11 真机验收仍待完成。

# 在 Windows 重绘 Claude Desktop 的原版窗口

此工具从 Linux 版 Claude Desktop 自带的开发者工具导入一帧已经提交的绘图，然后在 Windows 本地画出原版文字、路径、按钮和图层变换。当前验证范围是已退出账户时的离线提示窗口。它用于检查原版界面的绘制质量。持续导出后续帧、向 Claude 回传输入以及登录后的对话和 Artifacts 仍待验证。

## 在云端导出

1. 在专用 Linux 图形会话中正常启动 Anthropic 发布的 Claude Desktop。打开左上角应用菜单，依次选择 **Help → Troubleshooting → Enable Developer Mode…**，在原厂对话框确认。厂商的管理员策略决定该菜单是否可用。[Anthropic 的 Linux 安装说明](https://support.claude.com/en/articles/10065433-install-claude-desktop) · [开发模式入口](https://claude.com/docs/third-party/claude-desktop/in-app-configuration.md)
2. 在目标 Claude 窗口打开 **Developer → Show Dev Tools → Performance**。关闭 **Screenshots**。在 Performance 工具栏的齿轮中打开 **Enable advanced paint instrumentation (slow)**。[Chrome DevTools Performance 参考](https://developer.chrome.com/docs/devtools/performance/reference/)
3. 开始录制，在原窗口执行一次能触发重画的操作。验收用的是离线错误页的 **Reload** 按钮。在 Performance 面板停止并保存 `Trace-….json.gz`。发行版菜单另有 **Record Rendering Trace (rolling)**，本次实测该导出的内容是性能时间线，缺少此处需要的 SkPicture 数据。
4. 在拥有该仓库源码的云端环境运行以下命令。Python 3.11 或更高版本即可。生成的 CLRP 使用项目已有的窗口绘图格式。将该文件通过自己的私有文件通道交给 Windows 端。原始性能记录可能含有文档内容或脚本，整个导入可在源机器内完成。

```sh
python3 -m cloud_render.claude_trace Trace.json.gz claude.clrp
```

## 在 Windows 打开

在同版本源码目录运行以下命令。使用 x64 Python 和 x64 Node.js。查看器会优先使用发行包内的 Node，也可通过 `--node` 指定本机可执行文件。位于 `thin_client/vendor` 的官方 CanvasKit WASM 随仓库提供，绘制当前文件时无需联网。

```powershell
python -m experiments.inspect_native_frame .\claude.clrp
```

查看器建立原生 Win32 窗口，用 CanvasKit 执行绘图命令，显示完整客户区。程序在关闭窗口时退出。需要按像素核对时，可从同一份 CLRP 输出连续、逐行排列的 RGBA 像素，无文件头。

```powershell
node .\experiments\rasterize_application_frame.cjs .\thin_client\vendor .\claude.clrp .\drawing.rgba
```

## 确认图层来自目标窗口

导入器从 `TracingStartedInBrowser` 中核对原厂主框架 `file:///usr/lib/claude-desktop/resources/app.asar/.vite/renderer/main_window/index.html`，再以 `SetLayerTreeId` 确认渲染进程及合成树。它选择录制中最近一次满足完整条件的图层树。每个合成线程发布的 SkPicture 都要与该进程的某个明确 `UpdateLayer` 时间区间内的绘图数据逐字相同，还要能唯一对应树上的活动图层，由此排除其他进程及同时打开的开发者工具自身画面。这些校验使用所选导出的元数据。文件来源由操作者确认。

当前支持 Skia v110 的自包含矢量图层。导入器读取原记录中的轮廓、字形和字体，在标准 SkPicture 中记录原合成树的二维变换和裁切。透视、无法归属的绘图、外部字体、图像绘制和未经支持的画笔效果会产生导入错误。输出失败时保留已有的目标文件。字体只随产生它的私有绘图使用。[Skia 的 SkPicture 格式](https://github.com/google/skia/blob/main/src/core/SkPictureFlat.h)

## 2026-11-26 独立窗口核对

使用 Anthropic APT 签名密钥 `31DDDE24DDFAB679F42D7BD2BAA929FF1A7ECACE` 确认 Linux amd64 版 Claude Desktop 6.28899.6，包 SHA-256 为 `7e381601d86570149643d52e9b89c26575ab12fa788aa41ffdd4cca5b0638cf8`。实验在断网的临时用户配置中进行，通过厂商界面启用开发模式，保留发行包的沙盒。公开源码不含该次性能记录、厂商字体或记录中的页面内容。

导出的 33 次 SkPicture 发布来自两个进程。其中 21 次属于目标主框架，其余 12 次属于另一个进程。完整检查点含页面、旋转指示器、Reload 按钮和标题栏菜单四层。源 SkPicture 合计有四次 `DRAW_TEXT_BLOB` 和五次 `DRAW_PATH`，另有圆、矩形、圆角、裁切和矩阵操作。源层的图像绘制指令计数为零。保留四个原始绘图及合成矩阵后，CLRP 大小为 2,675,870 字节，SHA-256 为 `23528f1162f53ef08e3a16eb4cf9e55e1b3d1ddb92c2e7cb9d2b28a712f6dac9`。

| 1200 × 800 客户区对照 | 三个 RGB 分量均相同的像素 | 平均通道绝对误差，满值 255 |
| --- | ---: | ---: |
| Win64 CanvasKit 输出与独立采集的 Win32 窗口 | 960,000 / 960,000 | 0 |
| 原厂 Linux 客户区与独立采集的 Win32 窗口 | 950,551 / 960,000 | 0.196083 |

Windows 验收环境在 Wine 中运行 Windows x64 Python、Node 24.22.3 和 Win32 窗口程序。CanvasKit 的 RGBA 输出 SHA-256 为 `f73fd41a51c4d773ce025b79db82d61e61b6f948ccb0024c310b891a71a0379c`。源客户区和接收窗口的截图仅供测试比较。查看器的输入只有 CLRP。

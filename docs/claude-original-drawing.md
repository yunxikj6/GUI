# 在 Windows 重绘 Claude Desktop 的原版窗口

此工具从 Linux 版 Claude Desktop 自带的开发者工具导入已经提交的绘图，然后在 Windows 本地画出原版文字、路径、按钮和图层变换。当前验证范围是已退出账户时的离线提示窗口。它用于检查原版界面的绘制质量。一次保存可以包含多次真实操作的重绘。边录边显示、向 Claude 回传输入以及登录后的对话和 Artifacts 仍待验证。

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


## 从一次录制导出操作前后的画面

在同一段 Performance 录制内操作原窗口，然后停止并保存一次。以下命令列出该录制中具有完整提交与呈现关系的原厂帧编号。每个编号对应一个原厂状态，具体编号随录制变化。

```sh
python -m cloud_render.claude_trace Trace.json.gz --list-activations
python -m cloud_render.claude_trace Trace.json.gz before.clrp --activation 530
python -m cloud_render.claude_trace Trace.json.gz after.clrp --activation 540
```

主线程的 `Commit.frameSeqId` 与 `PipelineReporter.frame_sequence` 对应。导入器在同一合成线程及同一局部异步轨道里匹配完成的 `Activation`，再用 `ActivateLayerTree.frameId` 核对活动树的 `source_frame_number`。每个改变的显示列表都必须来自该次提交内对应的 `UpdateLayer`。只有相邻的已确认状态、图层号、图源引用和尺寸完全相同时才可沿用上一状态的绘图。遇到缺失的激活或图层归属时，导入器清空连续状态。每次生成 CLRP 仍验证支持的矢量指令和资源。

本次离线窗口录制有 14 次目标激活。其中 11 次保留了完整关联并可以分别导出。另 3 次缺少用于核对的合成轨道区间。两个独立的 Windows x64 客户区在编号 530 和 540 之间有 2,918 个像素改变，变化的包围框为 `[520,454]..[639,522]`。其中 2,805 个位于 Reload 按钮内，其余变化集中在重试指示器周围。两幅图共用一次原厂导出，分别由 Windows 本地执行原始文字、路径和矩阵指令。这项复验只涉及无账号窗口的一段已结束录制。

另一次测试从全新原厂进程开始。通过操作系统将原窗口调到 880 × 600，再在菜单、Reload 与重试指示器活动期间录制。四个原层都在该进程自己的主线程绘制时段里找到了相同的源字节。该次导出还提供了 21 个可独立验证的已激活状态。相同的源帧编号偶尔会由 Chromium 激活两次，导入器同时拒绝这两个歧义条目，并在重新取得完整绘图证据后恢复。

880 × 600 的 CLRP SHA-256 为 `0dae52bcd4f163680b12820689252028633e6eedbab6646b56d60370ac748480`。Win64 客户区与独立 CanvasKit 参照的 528,000 个像素全部相同。与原厂 Linux 客户区相比，518,544 个像素完全相同，平均 RGB 通道误差为 0.356985/255。Win32 窗口的原始 RGB SHA-256 为 `6458e8d59fd21af5323aea58a30eac6e15d4ea75f6b886ebb51774ae39281528`。

同一原厂版本的 **Developer → Record Performance Trace** 能自动写入文件。验收得到 1,219,714 字节、24,160 个包的 Perfetto 记录，其中显示列表、活动层树绘图快照及 SKP 数据的计数均为零。需要原绘图时应使用开启高级绘图记录的 Performance 面板。

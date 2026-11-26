# 连续接收 GNOME 与应用原厂绘图

在显示端，Win64 VirGL 实例按顺序执行 GNOME 的 Mesa 命令。收到有来源记录的应用窗口提交时，常驻 Windows Node.js 进程用 CanvasKit 运行原版 Electron 画出的 SkPicture，然后把生成的本机纹理交给 VirGL。重复提交可以在本次资源生命期内引用上一份原始绘图。云端的业务内容、原版应用、Mutter 和输入状态都留在源端。

本次复验使用同一次原厂签出窗口的两态记录。受保护窗口的原始位图、源图和完整原始命令均只供源端核验。Windows 显示进程收到独立的窗口身份清单及单向有序图形流。监听中的实际云应用、InstaBox 传输和 Windows 11 真机仍需共同验证。本入口用于复验，没有接管正式入口。

## 源与消费端

源端必须先验证每条应用 PUT 的完整归属。对这个窗口，证据包含官方二进制、进程开始时间、Wayland 连接、xdg_toplevel、真实原生提交、GPU 资源创建代次，以及该 PUT 在原始 vtest 流中的字节偏移。一次未配对的原生提交只产生等待记录。持续图形源可在 `stream_witnessed` 的 `inspect_put` 回调中等待对应见证。回调须对普通桌面传输给出明确判断，缺少判断会终止流。[源端见证](../experiments/mutter_app/README.md) · [顺序编码与消费](../experiments/live_application_surface.py)

客户端检查原始逻辑偏移、资源代次和提交顺序。原始桌面命令沿用 vtest。绑定后的源窗口像素一律拒绝，资源再次创建时必须重新核实身份。未配对的更新暂停显示整个桌面，直到同一窗口有新的获认绘图。缓存上限为当前窗口的一份原始绘图和一幅本地 RGBA 图。图形工作进程只继承运行所需的系统环境，通过专用管道处理绘图并在结束时回收。[常驻图形工作进程](../experiments/persistent_application_painter.py)

## 用同一次采集独立重跑

在源端运行下面的命令。输入目录需包含真实受控源按见证说明生成的签出状态记录。

```sh
python -m experiments.stream_attested_application EVIDENCE --manifest identity.json > graphics.bin
```

通过配对通道单独传递身份清单和图形流。显示端可从 stdin 持续接收图形，或对已保存的图形流执行相同命令。程序仅在本机执行固定的绘制操作。下例将完整本地扫描输出保存为顶行优先 BGRX，以及可独立比对的 result.json。

```sh
python -m experiments.receive_attested_application identity.json output --input graphics.bin --runtime-dir virgl-runtime --node node
```

本次数据来自隔离 Ubuntu 中运行的官方 ChatGPT 26.1120.50957。源端生成的 98 个 GPU 扫描摘要与独立原始命令回放逐一相同。去除 63 次应用纹理上传共 99,966,080 字节。53 个原生提交缺少获认绘图。消费端收到两份原厂绘图和八次复用，在 98 次扫描中暂停了 51 次。第 89 次扫描呈现欢迎页，第 98 次呈现登录方式。两张图的窗口外 323,600 个像素全部与同次来源相同。两态全桌面 RGB 通道平均绝对误差分别为 0.10480 和 0.10076，满量程为 255。[实测记录](measurements/application-surface/incremental.json)

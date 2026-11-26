# Win64 原厂应用绘图与输入验收

独立解压的 Windows x64 客户端运行在 Wine 上，云源角色运行原版 Ubuntu GNOME 和指定的 ChatGPT。测试员从原生 Windows 窗口点击 Ubuntu Dock，看到官方欢迎页，点击 “Sign in another way”，再打开 “OpenAI API Key”。原版表单自动聚焦。七个连续物理按键在同一个原控件中输入 `invalid`，云源逐次确认这七次输入。Windows 收到新一版原厂绘图后，独立窗口截图可读到完整的七个字符。

随后从 Windows 按 Ctrl+A，键入 `x`，再次按 Ctrl+A 并连续输入 `orbit`。Win32 窗口画出了完整的替换结果。焦点始终是原厂控件的第一个代次。验收以 Alt+F4 关闭了窗口，表单从头至尾保持未提交。这是签出状态的临时实例，所用文字全部为人工验收输入。[输入后的 Windows 窗口](input.png) · [替换后的 Windows 窗口](replace.png) · [实际输入记录](verification.json)

这项输入验收使用 Win64 客户端 ZIP `4570b7686a8a5648ca08f71585b5d973f106bb2d0b9fa073ff4ddbb0fb94c04f`、真实回环转发和独立的 Ubuntu 源角色。云源提交 `e967724` 在同一个原厂渲染任务里核对本次编辑的值与选区，在连续输入时保留已核验的焦点。此前服务在自身编辑后错误地变更代次，七次物理输入只能到达四次。

另一次在真实 InstaBox 云机上启动了同一官方应用，再将云源已经核对的两段原厂绘图与 GNOME 合成命令送到 Windows。此次在 Windows 上回放静态导出，输入发生在云源。Win64 进程及其真实窗口分别在第 163、171 次扫描显示了欢迎和登录方式。两次独立读取的 1280×900 窗口每个像素都与相应本机 VirGL 输出一致。[真云来源的欢迎页](cloud-welcome.png) · [真云来源的登录菜单](cloud-menu.png) · [像素与来源摘要](cloud-graphics.json)

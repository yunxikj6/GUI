# 云端工作台开发交接

运行启动器可直接进入 InstaBox 的完整桌面。开发网页项目工作台时，可以用本机模式完成一次“编辑 → 保存 → 查看差异 → 运行项目检查”。产品需求和完整场景见[需求与验收](docs/requirements.md)，用户启动说明见[README](README.md)。

## 当前基线

首个真机验证版本为 `v0.1.0`，对应提交 `dd835d363559cce5e89aa7a5402d8123499d78a1`。GitHub 的 `main` 包含这份实现和后续交接资料。原始发布资产 `cloud-workbench.zip` 为 334,015 字节，SHA-256 为 `f719081d013fe89ab809603cdc6449547e0144db8a364ac50d5a3cab4faf931f`。可在包目录运行 `sha256sum -c SHA256SUMS` 核验这份原始文件。[原始 ZIP](releases/v0.1.0/cloud-workbench.zip) · [校验文件](releases/v0.1.0/SHA256SUMS) · [0.1 发布页](https://github.com/yunxikj6/GUI/releases/tag/v0.1.0)

| 需要继续的工作 | 从哪里进入 | 已有证据 |
| --- | --- | --- |
| 本机界面、文件编辑和 Git 差异 | [前端](web/app.js)通过 [本机桥接](launcher.py) 的同源 `POST /api/rpc` 调用[文件服务](cloud/workspace.py) | 真机读写和冲突已测试；[浏览器回归](tests/test_browser.py)覆盖中文草稿、图片、窄屏和断线。 |
| 编程助手和任务恢复 | [任务执行器](cloud/tasks.py)启动独立子进程，把请求、状态和输出写入 SQLite。客户端从事件游标补读 | [任务测试](tests/test_tasks.py)覆盖真实 Git 检查、并发去重、分页、进程终止和模型 CLI 替身；真实 CLI 的会话协议可用回环模型服务验证。在线账号的历史结果见[认证记录](docs/validation-reviewed-edits.md)。 |
| 云机生命周期及适配其他主机 | [InstaBox 接入](instabox.py)负责选择、唤醒、按内容摘要安装工作进程，并用官方 HTTPS exec API 传输请求 | 真机挂起再恢复后，测试进程仍在，同一请求找回同一任务及全部 9 条事件。[实机记录](docs/validation.md) |
| 原有 Linux 图形软件 | [桌面代理](desktop.py)和[查看器](desktop_assets/viewer.js)提供 noVNC 实时连接、逐帧备用模式和输入交接 | 两种模式都操作过真实 Xfce；[桌面模块测试](tests/test_desktop.py)覆盖资源、连接和输入处理。 |
| 调研、性能实验 | [选型依据](docs/technology-review.md)、[GTK 实验脚本](experiments/broadway_probe.py)、[暖读测量](tools/measure.py)和[热缓存显示实验](experiments/cache_revalidation.py) | [云端计时样本](docs/measurements/first-cloud-run.json)、[云机验收数据](docs/measurements/instabox-acceptance.json)和[三轮 GTK 原始记录](docs/measurements/broadway-runs.json)。 |

## 复现起点

在 Linux 上准备 Python 3.10+ 和 Git。完整浏览器回归还需要 Python Playwright 和 Chromium；测试会从 PATH 寻找浏览器，也可用 `CLOUD_WORKSPACE_TEST_CHROMIUM` 指定可执行文件。原生协议回归从 PATH 寻找 Codex 和 Claude Code，测试各自创建临时 Git 项目。

```sh
python3 -m unittest discover -s tests -v
```

0.1 的 28 项测试全部通过。[首轮完整测试清单](docs/measurements/regression-v0.1.txt) 记录了各项结果。开发版新增了浏览器和原生 CLI 验收，缺少对应依赖时会跳过相关测试，终端仍可能显示 `OK`。[开发版验收](docs/validation-reviewed-edits.md)本机图形工作区可用下列命令单独启动，终端会打印回环地址：

```sh
python3 launcher.py --local --project "$(mktemp -d)/project" --no-browser
```

真机验证使用带桌面的 Linux `agent-browser` 实例。在本机启动器的隐藏提示中输入 InstaBox 密钥，或通过环境变量 `INSTABOX_API_KEY` 提供。模型认证属于云机上的 Codex 或 Claude CLI。只有一台实例时，运行 `python3 launcher.py` 查看完整云桌面。要在项目工作台打开首轮交付的源码，可运行下列命令。

```sh
python3 launcher.py --workbench --remote-project /data/cloud-workspace/source
```

这会打开首轮交付的 Git 源码工作副本。其中 `work` 分支固定在已部署的基线，后续从 GitHub 获取的新提交需单独同步。省略 `--remote-project` 会使用独立的示例仓库 `/data/cloud-workspace/project`；从多个实例中选择时加上 `--machine`。工作进程的版本目录在 `/data/cloud-workspace/runtime/`，原交付包在 `/data/cloud-workspace/releases/cloud-workbench.zip`。公开仓库只存放源码和可公开的结果；云机及模型访问使用各自的私有凭据。

## 修改时容易混淆的行为

- **文件新鲜度。** 云端用内容摘要作 `revision`。打开缓存文件先显示本地正文，再用 `ifRevision` 校验；保存携带编辑开始时的版本，旧版本会收到 `conflict`。文件缓存保存在当前页面内存。开发版将未保存的文本编辑备份到当前标签页，助手输入和未确认的任务请求也按工作区区分存储。[前端实现](web/app.js) · [文件测试](tests/test_workspace.py)
- **任务重试。** 一次用户提交复用一个 `requestId`，云端将它映射到持久任务。补读从最后确认的 `cursor` 继续；云端状态已经结束时，仍要读取到 `hasMore` 为假。每批事件上限为 256 KiB。任务摘要每页至多 80 项，用上次返回的 `nextBeforeTaskId` 取更早记录。事件历史在项目对应的 `${XDG_STATE_HOME:-~/.local/state}/cloudwork/` 中。[任务执行器](cloud/tasks.py)
- **首版边界。** 本机桥接监听 `127.0.0.1`。每个远端 RPC 都有控制面与启动 Python 的开销。文件服务每目录最多列出 400 项，文本上限 128 KiB，图片上限 256 KiB，差异上限 64 KiB。客户端区分本地反馈和云端确认。首轮测试实例闲置 30 分钟会暂停，免费套餐单次运行从 2 小时开始、活跃时最长 8 小时；恢复测试应分别覆盖暂停和停止。[文件服务](cloud/workspace.py) · [平台生命周期](https://instabox.sh/docs/machines/auto-pause)

## 当前开发版

Codex 和 Claude Code 成功后可用各自的原生会话继续追问。Codex 使用 CLI 返回的线程 ID。Claude 首轮由工作台分配 UUID，CLI 的初始化事件和成功结果都确认该 UUID 后才开放追问。两个提供方的回合关系分表保存，旧工作进程仍可读写原有 Codex 视图。早期临时会话仍可查看输出。[Claude 续聊与升级验收](docs/validation-claude-conversations.md)

单文件修改建议走单独的任务类型，生成器只接收所选文件的快照和修改要求。用户从差异视图作出决定后，服务端按原始内容摘要核对并应用修改。[任务执行器](cloud/tasks.py) · [提案与审批](cloud/proposals.py)

文件草稿保存到当前标签页的 sessionStorage。启动器在云机失联时也能确认项目身份，浏览器可恢复已编辑的内容。浏览器在工作区请求中带上已确认的 workspaceId，启动器切换项目时会拒绝旧页面的写入。草稿、重放和审批的故障验证见[开发版验收](docs/validation-reviewed-edits.md)。

任务取消由云机上的监督进程处理。浏览器读取服务端返回的 `canCancel`。收到 `cancelling` 后继续按游标补读，收到 `cancelled` 后仍要读完余下事件。升级前已在运行的执行器可以继续完成。新的普通 Codex 问答和历史会话续聊都会先核对当前项目与用户的工具配置。[任务取消与只读问答验收](docs/validation-task-cancellation.md)

任务列表按创建时间与 SQLite 行号倒序。当前任务在浏览的页面之外时，选择框会单独列出它。页位置按项目和标签页记录，事件轮询继续读当前任务自己的游标。[历史翻页验收](docs/validation-task-history.md)

长输出用 `events(tail=true)` 打开末段，向较早方向发送 `before`，向较新方向或恢复实时输出时发送 `after`。页边界取自服务端事件，多个 delta 可以合并成一行。浏览中的页和实时任务状态分别更新。旧工作进程继续提供原来的顺序补读。[长输出验收](docs/validation-output-windows.md)

根目录和子目录共用 `tree`，浏览器用上一页的 `nextCursor` 续读。每页最多 400 项，深层路径会按响应字节提前分页。目录被替换时，从目录开头重新读取。[大目录验收](docs/validation-tree-pagination.md)

文件服务和项目检查共用 `repository_dir`、`git_command` 和所选 Git 程序。关联工作树按 Git 登记的管理目录与回链定位。没有 Git 仓库或登记损坏时，状态请求仍返回文件工作区与具体原因。[关联工作树验收](docs/validation-git-worktrees.md)

Git 差异可按 `staged` 和 `unstaged` 分开读取。状态中的 `diffScopes` 声明支持的范围；没有该字段时先探测一次，再按回执决定是否显示合并差异。两层各有 64 KiB 补丁文本预算，浏览器按 800 行分页。[分层差异验收](docs/validation-diff-layers.md)

长文本只读预览和差异共用每页 800 行的本地分页。文件从服务器按原始正文读取，编辑器保存时保留未改动的换行。浏览长文件时可以用页码选择行段。[长文件预览验收](docs/validation-file-reader.md)

已确认的 Git 差异缓存在当前页面内存中，切换项目或确认仓库失效会清理缓存。旧服务的合并结果与新服务的分层结果互斥。任一层尚待校验时，界面保留该层的状态提示。[断线重读与时序验收](docs/validation-diff-cache.md)

实时桌面接管输入后提供双向文本剪贴板。查看器按 noVNC 协商结果选择发送方式，保留经典 RFB 无法表示的 Unicode 草稿。发送文字后，用户还需在目标应用里粘贴。云端发来的文本仅在用户主动复制时写入本机系统剪贴板。[实机往返与兼容性记录](docs/validation-desktop-clipboard.md)

## 下一轮从哪里开始

用户选择先直接进入完整云桌面。默认启动地址为 `/desktop/`，已有会话先复用。原版 Thunar 的新建文件夹操作已在独立 InstaBox 显示上核对到磁盘。项目工作台从 `/workbench/` 进入。[桌面入口与原软件实测](docs/validation-desktop-entry.md)

完整桌面现在支持适应窗口与原始大小。实时连接和逐帧查看使用同一比例选择。在观看或本地平移时，键鼠、触屏用来移动本机视窗。退出平移后，接管状态下的操作继续发送到云机。[原程序显示与点击验收](docs/validation-desktop-scale.md)

1. **核对完整桌面的日常操作。** 优先复查逐帧“发送文字”。在隔离的原版 Mousepad 中连续四次输入同一句含中文、空格与符号的文字时，现有 xdotool 指令均返回成功，但保存的 UTF-8 文件都有缺字。重新验收时应同时核对远端正文和保存的字节，再测输入法候选、剪贴板与断线恢复。用户可见的延迟按首窗、菜单响应与保存确认分别测量，保留客户端系统和网络条件。
2. **对照独立窗口。** 在同一测试文件上比较完整桌面和独立窗口。记录窗口所属进程、主窗口与对话框的关系、焦点切换和磁盘结果。分别注明本机实验或实际 InstaBox 云机。[实验条件与判据](docs/technology-review.md)
3. **保留现有入口的回归。** 网页工作台已有独立的文件、差异和编程助手流程。真实模型账号成功路径、其他客户端系统及公网弱网体验仍需各自的验收环境。

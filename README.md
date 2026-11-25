# 云端工作台

一个在本机浏览器中使用云端 Linux 项目的图形客户端。文件预览、编辑器、差异和编程助手输出由本机渲染。Linux 图形桌面可以在单独的窗口中打开，和这些工具访问同一台机器。

本分支支持 Codex 与 Claude Code 原生续聊、单文件修改审批、运行中任务取消和标签页草稿恢复。此前发布的独立下载包见 [0.1 发布页](https://github.com/yunxikj6/GUI/releases/tag/v0.1.0)。

![在本机工作台查看 InstaBox 上的 Python 文件和项目检查记录](docs/workbench.png)

## 连接云机

在本机安装 Python 3.10 或更新版本，解压源码，在含有 `launcher.py` 的目录运行下列命令。云机需要 InstaBox 的 Linux 实例、Python 3.10 或更新版本，以及 Git。图形桌面功能使用带桌面的模板。客户端运行只需 Python 标准库，桌面浏览器依赖已随包附带。

```sh
python3 launcher.py
```

Windows 可双击 `Start.cmd`，macOS 可双击 `Start.command` 或从终端运行上面的命令。输入 InstaBox 密钥时终端不会回显，已配置的 `INSTABOX_API_KEY` 也可使用。程序会打开本机浏览器。若有多台实例，用 `--machine m_…` 选择。可用 `--no-browser` 仅打印本机地址。请保持启动器运行，关闭它会断开本机连接；已接受的任务由云机持有。

首次连接会启动或恢复所选实例，并在 `/data/cloud-workspace/project` 中准备一个独立的示例 Git 项目。有现成项目可指定其在云机上的绝对路径。下面的命令读取已有目录：

```sh
python3 launcher.py --remote-project /data/workspace/my-project
```

用 `git worktree add` 创建的关联工作树也可直接作为项目打开。请把 `--remote-project` 指向该工作树的根目录。分支、Git 差异和项目检查都读取这份工作树自己的索引。分离 HEAD 时，侧栏显示当前提交的短标识。打开普通目录仍可浏览和编辑文件。

工作台默认从云机的 `/usr/local/bin` 和系统目录查找 Git。需要指定版本时，在运行启动器前将 `CLOUD_WORKSPACE_GIT` 设为所选机器上 Git 可执行文件的绝对路径。本机模式使用本机路径，InstaBox 模式使用云机路径。

密钥只交给本机启动器。云机工作进程、网页、交付包均不需要保留账户密钥。客户端监听 `127.0.0.1`，校验 HTTP Host 和网页 Origin。实例通信通过 InstaBox 官方 HTTPS API 完成。云机不用额外开放端口。选择项目意味着允许本机查看及编辑该目录中支持的文件。

## 首次使用

1. 在左侧打开 `README.md`。只读预览中的长文本可按行号范围翻页，翻页使用已收到的正文。选择“编辑文件”，输入一行文字并保存。切换到“Git 差异”，在“已暂存”核对准备提交的内容，在“未暂存（含未跟踪）”查看之后仍留在工作区的改动。每层可单独折叠、重试和翻页。已读补丁保留在当前标签页内存中。断线后切回差异，仍能查看上次收到的正文，界面会标注“缓存未校验”。恢复连接后，可按层重试。未保存的文件草稿按标签页、工作区和文件保存。保持启动器运行，在同一标签页刷新后可以继续编辑。若云端文件已有新版本，界面会保留草稿并提示冲突。浏览器存储不可用时，界面会提示先保存。
2. 在“云端任务”点击“项目检查”。检查会读取 Git 索引，并分别检查工作区与暂存区。要停止某项排队中或运行中的任务，在它的记录中点击“取消任务”。云机收回该任务的子进程后会显示“已取消”。此前收到的输出可以继续阅读，刷新后也能找回。先一步完成的任务会显示实际结果。
3. 选择 Codex 或 Claude Code，输入希望它分析的问题。模型回答并确认会话后，打开“继续此对话”输入追问。用“上一轮”和“下一轮”回看问答。例如，在 Claude 回合追问时，即使上方切换到了 Codex，回复仍由 Claude 生成。两种 CLI 都把原生会话保存在云机上。旧版 Claude 单轮任务可以继续查看输出。
4. 需要改文件时，打开目标文本，选择 Codex 的修改建议并说明改动。任务记录中会显示目标路径和建议差异。你可以应用或拒绝建议。文件在等待期间被别人改过时，工作台会保留差异并提示重新生成。单文件的原文和建议内容各限 24 KiB。
5. 点“Linux 桌面”打开 Xfce，然后选择“接管输入”。实时连接中可以打开“剪贴板”，把本机文字粘贴到“发送到云端的文本”，点击“发送到云端”后再到云端应用粘贴。从云端取回文字时，先在应用里复制，再点查看器中的“复制到本机”。浏览器拒绝写入时，查看器会选中文字供手动复制。用完可交还控制。桌面默认使用 noVNC，连接受限时支持逐帧模式。

任务记录每页显示 80 项。点“较早”查找以前的任务，用“较新”往回翻，从第三页起也可以直接点“最近记录”。翻页时，正在阅读的任务和追问草稿会保留。选好一项旧任务后，在同一标签页刷新即可回到该页和该任务。

文件树按目录优先、名称排序。大目录底部有“加载更多”，根目录和展开的子目录都能分别继续读取。翻页时连接中断，可以从原位置重试。其他程序移动或新增文件后，刷新目录可看到当前清单。

长任务会先显示最新输出。用“较早输出”和“较新输出”逐段阅读，点“回到最新输出”可再次跟随任务。向上滚动阅读或选中文字时，新到的记录会保留在云机，当前文字和选区仍可阅读。在同一标签页刷新会重新打开已保存的历史输出窗口。

Claude Code 问答向模型开放读取和搜索项目的 `Read`、`Glob`、`Grep`。任务记录会在调用时显示工具名称，刷新后仍能查看。工作台为本次调用指定空的 MCP 配置，并用安全模式停用普通用户、项目和插件的执行钩子。主机管理员配置的受管钩子仍可运行。云机需安装支持 `--safe-mode` 的 Claude Code，已验证版本为 2.1.288。Codex 问答保留云机模型和登录配置，使用只读沙箱内的项目命令，并为该次调用关闭外部 MCP 工具、应用扩展、插件和钩子。

## 实现

| 组成 | 职责 |
| --- | --- |
| `launcher.py`、`web/` | 本机 HTTP 服务、同源校验、工作区界面、草稿及重连 |
| `instabox.py` | 选择与唤醒机器、按内容摘要部署工作进程、调用官方 API |
| `cloud/workspace.py` | 受限的文件访问、按文件版本保存、Git 差异与工作区请求 |
| `cloud/tasks.py` | 云端任务、进程回收、SQLite 记录、请求去重、历史翻页和事件补读 |
| `cloud/codex_tools.py` | 核对并约束本次 Codex 调用可用的外部工具 |
| `cloud/proposals.py` | 单文件修改建议、完整差异、按文件版本审批写入 |
| `desktop.py`、`desktop_assets/` | 私有桌面连接、noVNC WebSocket 代理、备用截屏和输入 |

应用工作进程按版本安装在云机的 `/data/cloud-workspace/runtime/`。任务数据库位于远端用户的 `${XDG_STATE_HOME:-~/.local/state}/cloudwork/`，按项目目录隔离。项目文件和模型登录归云机用户管理。当前图形客户端连接单台机器、一个工作目录；切换机器或项目可重启启动器。云机休眠期间任务暂停，恢复后以实际进程状态为准。

## 开发与验证

本机 Linux 开发模式使用相同文件与任务代码，需要 Git。先运行回归，再启动一个临时工作区：

```sh
python3 -m unittest discover -s tests -v
python3 launcher.py --local --project "$(mktemp -d)/project"
```

保持启动器运行，在另一个终端执行 `python3 tools/measure.py --file README.md --samples 15` 可测量当前模式的文件读取往返。浏览器回归需要 Python Playwright 和 Chromium。原生会话回归分别调用已安装的 Codex 和 Claude Code，修改建议回归使用 Codex。测试使用临时配置和回环模型服务，可在没有模型账户的环境执行。缺少相应依赖时，测试会标记跳过。

- [架构调研与选型依据](docs/technology-review.md)
- [GTK 图形工具包实验](docs/toolkit-experiment.md)
- [实时桌面双向文本剪贴板验收](docs/validation-desktop-clipboard.md)
- [首版实机验收和性能记录](docs/validation.md)
- [续聊、草稿与修改审批验收](docs/validation-reviewed-edits.md)
- [任务取消与只读问答验收](docs/validation-task-cancellation.md)
- [Claude Code 续聊与升级验收](docs/validation-claude-conversations.md)
- [任务历史翻页与云机升级验收](docs/validation-task-history.md)
- [长任务输出与实时回放验收](docs/validation-output-windows.md)
- [大目录浏览与云机验收](docs/validation-tree-pagination.md)
- [Git 关联工作树与项目检查验收](docs/validation-git-worktrees.md)
- [已暂存与未暂存差异验收](docs/validation-diff-layers.md)
- [断线重读 Git 差异验收](docs/validation-diff-cache.md)
- [长文件只读预览验收](docs/validation-file-reader.md)
- [项目需求](docs/requirements.md)和[开发交接](AGENTS.md)
- [桌面查看器第三方许可](desktop_assets/NOTICE.txt)

# 在官方 Claude 和 ChatGPT 客户端使用 InstaBox 项目

在 Claude Desktop 对话或 ChatGPT Desktop 的本地 Codex 项目中，可以列出云目录、读取文本并查看 Git 状态。文件和用来读取它的 Python 进程都在选定的 InstaBox Linux 实例上。你选择调用的工具结果会进入官方客户端的对话上下文。账户、订阅、对话记录和工具授权由各自的官方客户端及组织管理。

要手动附上一份文本，可以先在 GNOME 的 **InstaBox 项目文件** 中取回它。回到原厂应用，使用会话中的附件按钮。如果出现 Ubuntu 的系统文件选择器，点 **最近使用**，选择刚取回的文件。应用收到的是这份本地副本，上传与发送由原厂应用处理。[在 Ubuntu 中取回文件](native-project-files.md#日常操作) · [Claude 的附件说明](https://support.claude.com/en/articles/8241126-upload-files-to-claude)

## 在 Ubuntu 桌面连接

1. 从 GNOME 顶栏进入“活动”，搜索 **连接 InstaBox 到官方客户端**（英文 **Connect InstaBox to desktop apps**）。选择 **Claude** 或 **ChatGPT**。ChatGPT 使用已安装的 Ubuntu 官方桌面程序中的本地 Codex 项目。
2. 已在“InstaBox 项目文件”中选过云机和目录时，这里会沿用该项目。首次绑定会引导你选择云机和目录，并把凭据存入 Ubuntu 用户密钥环。核对确认页上的客户端、机器和绝对路径，选择读取权限，再点 **连接此项目**。已有合适的 Python 环境会直接复用，首次需要下载连接组件时，确认页会说明。若从旧版升级并提示缺少 `python3-venv`，在 Windows 中再运行一次新版 `Setup.cmd` 即可补装。
3. 完全退出并重新打开所选客户端。在 Claude 的 **Settings → Developer → Local MCP servers** 或 ChatGPT 本地 Codex 项目的 **MCP** 设置中查看 `instabox-workspace`。可在已登录的项目会话中要求“读取这个云项目的 README.md”，并在客户端核对工具授权。实际返回的项目内容可进入这次厂商会话。

每次只配置所选客户端。要让另一个客户端访问该项目，重新打开同一入口并选择它。要切换云项目，先在“InstaBox 项目文件”的“连接 InstaBox 项目”中更改项目，再为需要切换的客户端运行上述步骤。迁移解压目录后也可重新运行以更新启动路径。桌面入口使用系统密钥环和只读工具。需要开启保存时可使用下面的手工配置。

首次连接的确认框来自 GNOME。确认后，ChatGPT 通过软件包自带的 Codex 设置接口登记连接，Claude 使用当前用户的 `claude_desktop_config.json`。如果已存在用户自行配置的同名服务，工作区会提示检查该项。[项目授权框实拍](native-client-git-review.png) · [配置和读回记录](measurements/native-client-connections.json)

切换项目时，ChatGPT 的设置接口可能重新排布该服务启动参数数组里的注释。如果另一个程序同时编辑设置并出现冲突，请先核对客户端的当前设置再重试。Claude 的配置目录如果位于不支持原子交换的文件系统上，可以在下面的手工配置步骤中合并已有文件。

## Ubuntu 手工配置

使用已登录的 Ubuntu 桌面用户，在解压的项目目录中运行以下命令。需要 Python 3.10 或更新版本、对应的 venv 组件和提供 Secret Service 的系统密钥环（例如 GNOME Keyring）。云机需已运行，并装有 Python 3。InstaBox 密钥需要能读取机器列表并调用该实例的 exec 接口。

```sh
python3 -m venv .venv-mcp
.venv-mcp/bin/python -m pip install -r requirements-mcp.txt
.venv-mcp/bin/python workspace_mcp.py --machine m_example --project "/data/my-project" --store-key
.venv-mcp/bin/python workspace_mcp.py --machine m_example --project "/data/my-project" --print-config
```

将示例机器标识和目录换成自己的现有项目。默认凭据源在 Linux 只使用当前用户 D-Bus 会话的 Secret Service。按提示解锁登录密钥环。若终端或 Claude Desktop 无法访问密钥环，请从同一个已登录的桌面用户会话启动，确认密钥环服务可用并解锁。适配器会在凭据库不可用时报错。宿主过滤会话环境变量时，适配器会检查当前用户私有运行目录中的标准总线套接字。由本工作区启动的嵌套 GNOME 会话也可通过此套接字访问同一个用户密钥环。文件管理器和通知使用嵌套桌面自己的会话总线。Ubuntu 的“密码和密钥”可管理该机器对应的 “InstaBox Workspace MCP” 条目。

## Windows 配置

在装有 Python 3.10 或更新版本以及 Claude Desktop 的电脑上解压本项目。从项目目录运行以下 PowerShell 命令。把机器标识和目录换成已有实例和已有项目。该云机需要处于运行状态，安装了 Python 3，并且所用 InstaBox 密钥能够读取机器列表和调用该实例的 exec 接口。

```powershell
py -3 -m venv .venv-mcp
.\.venv-mcp\Scripts\python.exe -m pip install -r requirements-mcp.txt
.\.venv-mcp\Scripts\python.exe workspace_mcp.py --machine m_example --project "/data/my-project" --store-key
.\.venv-mcp\Scripts\python.exe workspace_mcp.py --machine m_example --project "/data/my-project" --print-config
```

按提示输入密钥。Windows 会以当前用户身份将它保存至 Windows 凭据管理器。macOS 使用钥匙串，在终端运行同样的步骤，将 `py -3` 换为 `python3`，将解释器路径换为 `.venv-mcp/bin/python`。同一台机器轮换密钥时重新执行 `--store-key` 并重新启动官方客户端的 MCP 连接。也可在系统凭据管理界面删除该机器对应的 “InstaBox Workspace MCP” 记录。

在 Claude Desktop 的 **Settings → Developer → Edit Config** 中打开配置文件（Linux 为 `~/.config/Claude/claude_desktop_config.json`）。将生成的 `instabox-workspace` 项合并进已有的 `mcpServers`，保留其他服务器。完全退出并重新启动 Claude Desktop，开启对应工具。在新对话中输入“列出云项目根目录，然后读取 README.md”，按官方客户端的授权提示操作。绝对路径由配置命令生成，移动程序或更换项目后应重新生成。组织管理员可以禁用本地 MCP。[MCP 本地服务器配置](https://modelcontextprotocol.io/docs/develop/connect-local-servers) · [Claude Desktop 管理策略](https://support.claude.com/en/articles/12622667-enterprise-configuration-for-claude-desktop)

适配器通过标准输入和输出与官方客户端通信。无需运行本仓库的桌面启动器。也支持由可信父进程注入环境变量 `INSTABOX_API_KEY`，在生成配置时添加 `--credential-source environment`。桌面程序需能把该变量传给子进程；用环境变量模式时应从实际启动方式验证。项目目录和机器标识由本地配置固定。

在全新用户配置中，官方 Claude Desktop 6.28899.6 启动时访问了配置文件。停留欢迎页观察 40 秒，期间未启动配置的 stdio 进程。同一配置的独立协议测试完成了握手和三项只读工具发现。要验收对话中实际调用云项目工具，仍需登录官方客户端并完成授权。

## ChatGPT Desktop 的本地 Codex 项目

使用上述同一 Python 环境和系统凭据，为本机的 Codex 主机生成 TOML 片段。

```sh
.venv-mcp/bin/python workspace_mcp.py --machine m_example --project "/data/my-project" --host codex --print-config
```

将生成的 `[mcp_servers.instabox-workspace]` 配置加入本机 `~/.codex/config.toml`，或放在已信任项目的 `.codex/config.toml`。在 ChatGPT Desktop 的本地 Codex 项目中打开 MCP 设置，确认工具已启用；同一主机的 Codex CLI 可用 `codex mcp list` 和 `codex mcp get instabox-workspace` 核对配置。默认 stdio 进程位于本机，使用本机的 Python、凭据库和出站网络。工具授权由官方客户端及组织策略控制。ChatGPT Work 的托管会话使用各自环境中的连接。[ChatGPT 本地 MCP 配置](https://learn.chatgpt.com/docs/extend/mcp) · [ChatGPT 开发者设置](https://learn.chatgpt.com/docs/developer-settings)

在 Ubuntu 上，已用 OpenAI 官方 ChatGPT Linux 软件包自带的 Codex 组件读取授权的 InstaBox 项目。组件通过系统密钥环连接，列出三项只读工具，查询云端 Git 状态，读回 3,549 字节的文件并与对应提交逐字节比对。生成的 TOML 允许子进程继承系统 HTTPS 代理和证书设置，代理地址及凭据保留在运行环境中。这项测试没有发送模型请求。登录后的聊天界面调用仍待验收。[OpenAI Linux 客户端](https://learn.chatgpt.com/docs/linux/linux-app) · [调用与文件实测记录](measurements/native-linux-clients.json)

## 在原厂客户端审阅云端代码

在 GNOME 的“连接 InstaBox 到官方客户端”里选好应用和项目，确认页默认提供基础读取。选 **包含 Git 差异**，保存并完全重启所选客户端，就可以向已登录的会话提问，例如“解释这个项目最近一次提交”，或“分别查看已暂存和未暂存的改动”。云机上的 Git 计算补丁，结果可能包含已删除的行以及未跟踪文本文件的内容。重新打开同一入口，选择基础权限、保存并重启，即可收回差异工具。已经返回到厂商会话的内容仍由该会话管理。

手工配置时，在原有的 `--print-config` 命令中加入 `--allow-diff`。这项权限只读，与允许保存文件的 `--allow-save` 分别设置。差异工具每次读取一个范围，补丁正文最多 64 KiB。若 `truncated` 为真，本次结果只覆盖补丁的开头。

| 请求范围 | 云机上比较的内容 |
| --- | --- |
| `staged` | 当前提交与暂存区。尚无提交时也可以查看已暂存的新文件。 |
| `unstaged` | 暂存区与工作树。还会预览未被 Git 忽略的未跟踪文本。 |
| `last_commit` | 最近一次提交与其第一父提交。结果附提交号，根提交则与空树比较。 |

各范围分别采样。切换分支或再次编辑文件后，下一次读取会反映新的状态。仓库尚无提交或浅克隆缺少父对象时，最近提交查询会报告对应状态。[原厂组件和云机结果核对](measurements/native-git-review.json)

## 默认工具与可选权限

- `list_workspace_directory` 按项目相对路径列出目录。根目录可省略路径；后续页使用返回的 `nextCursor` 及原路径。每页最多 400 项。
- `read_workspace_text` 读取不超过 128 KiB 的 UTF-8 文本，附带路径、字节数和 SHA-256 版本。云文件接口沿用现有路径和符号链接限制。
- `get_workspace_git_status` 在云机运行固定的 Git 状态检查，返回分支、暂存和工作树状态及未跟踪项。每次最多 400 项或 64 KiB 的 Git 输出。截断时 `truncated` 为真。无仓库与损坏的仓库分别返回 `gitState=none` 和 `gitState=error`。
- 选用 Git 差异后会增加 `get_workspace_git_diff` 工具，必填参数 `scope` 对应上表中的三个范围。
- 需要编辑时，在生成配置的命令中加 `--allow-save`，将新配置片段更新至官方客户端并重新启动连接。此时才提供 `save_workspace_text`。保存需要同一连接已读取的既有文件及其 SHA-256 版本。工具按该版本核对云端文件，并执行原子替换。版本过期时返回 `conflict`，应重新读取、核对改动后再决定是否保存。Codex 的可写配置会为该工具请求宿主确认。去掉该参数并重启连接即可恢复只读模式。
- 工作区代码在云机内存中运行。云机暂停、项目不存在、文本过大或属于二进制格式时，工具会向官方客户端返回错误。

## ChatGPT Work 和托管的自定义连接

ChatGPT Work 和托管的自定义 MCP 连接由其服务端发起。在当前 **Plugins → Add custom MCP server** 中，可以选择 HTTPS 服务或官方 Secure MCP Tunnel。Tunnel 客户端可在本机启动 stdio 适配器，并通过出站连接访问它。连接时需分别确认目标 ChatGPT 工作区、Platform 组织的 Tunnel 权限以及工具访问授权。当前适配器直接使用配置者的单个 InstaBox 身份，适合个人专用连接。多人共享需要按成员控制可读数据和云机身份。[ChatGPT 自定义 MCP 连接](https://help.openai.com/en/articles/12584461-connecting-custom-mcp-servers-in-chatgpt) · [Secure MCP Tunnel](https://developers.openai.com/api/docs/guides/secure-mcp-tunnels)

官方说明将 ChatGPT Pro 的自定义连接限制为读取或获取数据；Business 由管理员或所有者创建并发布，Enterprise/Edu 可按角色授权成员创建、由管理员或所有者发布。帐户界面和工作区策略决定具体可用入口；官方目前将自定义服务器连接设置列为 Web 功能。本仓库的 stdio 验收通过原版 MCP SDK 完成，Claude Desktop 的账户授权和 ChatGPT Tunnel 的注册、关联及登录属于各自的官方流程。[OpenAI 方案和权限说明](https://help.openai.com/en/articles/12584461-connecting-custom-mcp-servers-in-chatgpt)

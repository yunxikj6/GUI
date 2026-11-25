# 在官方 Claude 和 ChatGPT 客户端使用 InstaBox 项目

在 Claude Desktop 对话或 ChatGPT Desktop 的本地 Codex 项目中，可以列出云目录、读取文本并查看 Git 状态。文件和用来读取它的 Python 进程都在选定的 InstaBox Linux 实例上。你选择调用的工具结果会进入官方客户端的对话上下文。账户、订阅、对话记录和工具授权由各自的官方客户端及组织管理。

## Ubuntu 配置

使用已登录的 Ubuntu 桌面用户，在解压的项目目录中运行以下命令。需要 Python 3.10 或更新版本、对应的 venv 组件和提供 Secret Service 的系统密钥环（例如 GNOME Keyring）。云机需已运行，并装有 Python 3。InstaBox 密钥需要能读取机器列表并调用该实例的 exec 接口。

```sh
python3 -m venv .venv-mcp
.venv-mcp/bin/python -m pip install -r requirements-mcp.txt
.venv-mcp/bin/python workspace_mcp.py --machine m_example --project "/data/my-project" --store-key
.venv-mcp/bin/python workspace_mcp.py --machine m_example --project "/data/my-project" --print-config
```

将示例机器标识和目录换成自己的现有项目。默认凭据源在 Linux 只使用当前用户 D-Bus 会话的 Secret Service。按提示解锁登录密钥环。若终端或 Claude Desktop 无法访问密钥环，请从同一个已登录的桌面用户会话启动，确认密钥环服务可用并解锁。适配器会在凭据库不可用时报错。宿主过滤会话环境变量时，适配器会检查当前用户的私有运行目录中的标准总线套接字。Ubuntu 的“密码和密钥”可管理该机器对应的 “InstaBox Workspace MCP” 条目。

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

## 默认只读工具和可选保存

- `list_workspace_directory` 按项目相对路径列出目录。根目录可省略路径；后续页使用返回的 `nextCursor` 及原路径。每页最多 400 项。
- `read_workspace_text` 读取不超过 128 KiB 的 UTF-8 文本，附带路径、字节数和 SHA-256 版本。云文件接口沿用现有路径和符号链接限制。
- `get_workspace_git_status` 在云机运行固定的 Git 状态检查，返回分支、暂存和工作树状态及未跟踪项。每次最多 400 项或 64 KiB 的 Git 输出。截断时 `truncated` 为真。无仓库与损坏的仓库分别返回 `gitState=none` 和 `gitState=error`。
- 需要编辑时，在生成配置的命令中加 `--allow-save`，将新配置片段更新至官方客户端并重新启动连接。此时才提供 `save_workspace_text`。保存需要同一连接已读取的既有文件及其 SHA-256 版本。工具按该版本核对云端文件，并执行原子替换。版本过期时返回 `conflict`，应重新读取、核对改动后再决定是否保存。Codex 的可写配置会为该工具请求宿主确认。去掉该参数并重启连接即可恢复只读模式。
- 工作区代码在云机内存中运行。云机暂停、项目不存在、文本过大或属于二进制格式时，工具会向官方客户端返回错误。

## ChatGPT Work 和托管的自定义连接

ChatGPT Work 和托管的自定义 MCP 连接由其服务端发起。在当前 **Plugins → Add custom MCP server** 中，可以选择 HTTPS 服务或官方 Secure MCP Tunnel。Tunnel 客户端可在本机启动 stdio 适配器，并通过出站连接访问它。连接时需分别确认目标 ChatGPT 工作区、Platform 组织的 Tunnel 权限以及工具访问授权。当前适配器直接使用配置者的单个 InstaBox 身份，适合个人专用连接。多人共享需要按成员控制可读数据和云机身份。[ChatGPT 自定义 MCP 连接](https://help.openai.com/en/articles/12584461-connecting-custom-mcp-servers-in-chatgpt) · [Secure MCP Tunnel](https://developers.openai.com/api/docs/guides/secure-mcp-tunnels)

官方说明将 ChatGPT Pro 的自定义连接限制为读取或获取数据；Business 由管理员或所有者创建并发布，Enterprise/Edu 可按角色授权成员创建、由管理员或所有者发布。帐户界面和工作区策略决定具体可用入口；官方目前将自定义服务器连接设置列为 Web 功能。本仓库的 stdio 验收通过原版 MCP SDK 完成，Claude Desktop 的账户授权和 ChatGPT Tunnel 的注册、关联及登录属于各自的官方流程。[OpenAI 方案和权限说明](https://help.openai.com/en/articles/12584461-connecting-custom-mcp-servers-in-chatgpt)

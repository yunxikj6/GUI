"""Read an existing InstaBox workspace from an MCP host over standard input and output."""
from __future__ import annotations

import argparse
import asyncio
from collections import OrderedDict
import getpass
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import subprocess
import sys
from urllib.parse import quote_from_bytes
import warnings

from instabox import BridgeError, InstaBox, json_bytes

VAULT_SERVICE = "InstaBox Workspace MCP"
LIST = "list_workspace_directory"
READ = "read_workspace_text"
GIT_STATUS = "get_workspace_git_status"
SAVE = "save_workspace_text"


def private_user_bus():
    uid = os.getuid()
    runtime = Path(os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{uid}")
    # WSLg may export its shared display runtime while systemd's per-user bus lives separately.
    if os.environ.get("CLOUDWORKSPACE_NESTED_GNOME") == "1" and runtime == Path("/mnt/wslg/runtime-dir"):
        runtime = Path(f"/run/user/{uid}")
    if not runtime.is_absolute():
        raise ValueError("私有会话总线不可用。")
    info = runtime.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != uid or info.st_mode & 0o077:
        raise ValueError("私有会话总线不可用。")
    bus = runtime / "bus"
    socket_info = bus.lstat()
    if not stat.S_ISSOCK(socket_info.st_mode) or socket_info.st_uid != uid:
        raise ValueError("私有会话总线不可用。")
    return "unix:path=" + quote_from_bytes(os.fsencode(bus), safe="/")


class UserBusVault:
    """Run only the upstream keyring operation on the user's stable bus, leaving GUI IPC intact."""
    _worker = """import json, sys
from keyring.backends.SecretService import Keyring
method, arguments = json.load(sys.stdin)
result = getattr(Keyring(), method)(*arguments)
json.dump(result, sys.stdout, ensure_ascii=True)
"""

    def __init__(self, address):
        self.address = address

    def _request(self, method, *arguments):
        # A key travels on anonymous pipes. Neither Python argv nor the environment carries it.
        env = {**os.environ, "DBUS_SESSION_BUS_ADDRESS": self.address}
        env.pop("INSTABOX_API_KEY", None)
        reply = subprocess.run([sys.executable, "-B", "-c", self._worker], cwd="/", env=env,
                               input=json.dumps([method, arguments]).encode("ascii"),
                               stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        if reply.returncode:
            raise RuntimeError("无法访问用户密钥环，或解锁已取消。")
        return json.loads(reply.stdout)

    def get_password(self, service, username):
        return self._request("get_password", service, username)

    def set_password(self, service, username, password):
        self._request("set_password", service, username, password)

    def delete_password(self, service, username):
        self._request("delete_password", service, username)


def system_vault():
    if sys.platform.startswith("linux"):
        current = os.environ.get("DBUS_SESSION_BUS_ADDRESS")
        if os.environ.get("CLOUDWORKSPACE_NESTED_GNOME") == "1":
            try:
                address = private_user_bus()
            except (OSError, ValueError):
                if not current:
                    raise
            else:
                if not current or current.split(",", 1)[0] != address:
                    return UserBusVault(address)
        if not current:
            # A desktop MCP host may filter its bus variable. Recover the ordinary user socket.
            os.environ["DBUS_SESSION_BUS_ADDRESS"] = private_user_bus()
        from keyring.backends.SecretService import Keyring
        return Keyring()
    if sys.platform == "win32":
        from keyring.backends.Windows import WinVaultKeyring
        return WinVaultKeyring()
    if sys.platform == "darwin":
        from keyring.backends.macOS import Keyring
        return Keyring()
    raise ValueError("此系统请使用 --credential-source environment，从父进程继承 INSTABOX_API_KEY。")


def vault_help():
    if sys.platform.startswith("linux"):
        return "无法访问 Secret Service。请在同一桌面用户会话中解锁登录密钥环，或从可信父进程使用环境变量模式。"
    return "无法访问系统凭据库。请解锁凭据库，或从可信父进程使用环境变量模式。"


def read_secret(machine, source):
    if source == "environment":
        key = os.environ.get("INSTABOX_API_KEY")
    else:
        try:
            key = system_vault().get_password(VAULT_SERVICE, machine)
        except Exception:
            raise BridgeError("credential_store", vault_help()) from None
    if not key or not key.strip():
        raise BridgeError("missing_key", "没有可用的 InstaBox 凭据，请在本机配置后重试。")
    return key


class CloudFiles:
    def __init__(self, machine, project, source, *, allow_save=False):
        self.machine = machine
        self.project = project
        self.source = source
        self.allow_save = allow_save
        self.read_revisions = OrderedDict()
        self.transport = None
        self.lock = asyncio.Lock()

    async def call(self, name, arguments):
        if name == LIST:
            request = {"op": "tree", "path": arguments.get("path", "")}
            if "cursor" in arguments:
                request["cursor"] = arguments["cursor"]
        elif name == READ:
            request = {"op": "file", "path": arguments["path"]}
        elif name == GIT_STATUS:
            request = {"op": "status", "scope": "git"}
        elif name == SAVE and self.allow_save:
            path, revision = arguments["path"], arguments["revision"]
            if path not in self.read_revisions:
                raise BridgeError("read_required", "请先在当前会话中读取要保存的现有文本文件。")
            if self.read_revisions[path] != revision:
                raise BridgeError("conflict", "版本与当前会话的读取记录不同，请重新读取文件。")
            request = {"op": "save", "path": path, "revision": revision, "content": arguments["content"]}
        else:
            raise ValueError("此工具未启用。")
        async with self.lock:
            if self.transport is None:
                key = await asyncio.to_thread(read_secret, self.machine, self.source)
                self.transport = await asyncio.to_thread(InstaBox, key, self.machine, self.project)
        data = await asyncio.to_thread(self.transport.file_request, request)
        if name == READ and data.get("kind") != "text":
            raise BridgeError("unsupported_file", "此工具仅读取 UTF-8 文本。")
        if name == READ and self.allow_save:
            try:
                self.read_revisions[data["path"]] = data["revision"]
                self.read_revisions.move_to_end(data["path"])
            except (KeyError, TypeError):
                raise BridgeError("invalid_response", "未取得有效的工作区响应。") from None
            if len(self.read_revisions) > 1024:
                self.read_revisions.popitem(last=False)
        return data


async def serve(files):
    from mcp import types
    from mcp.server.lowlevel import Server
    from mcp.server.stdio import stdio_server

    server = Server("instabox-workspace", version="1.0", instructions="操作本机配置指定的 InstaBox 项目。保存前读取目标文件并核对改动。" if files.allow_save else "只读访问指定的 InstaBox 项目。")
    hints = types.ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False)
    path = {"type": "string", "maxLength": 4096, "description": "相对于项目根目录的路径。"}
    tools = [
        types.Tool(name=LIST, description="列出项目目录。根目录请省略 path。若返回 nextCursor，保持 path 原样并传入 cursor 读取下一页。",
                   annotations=hints, inputSchema={"type": "object", "additionalProperties": False, "properties": {
                       "path": path, "cursor": {"type": "string", "minLength": 1, "maxLength": 8192}}}),
        types.Tool(name=READ, description="读取一个最多 128 KiB 的 UTF-8 文本文件，返回原文、路径、大小和版本。",
                   annotations=hints, inputSchema={"type": "object", "additionalProperties": False,
                                                  "required": ["path"], "properties": {"path": {**path, "minLength": 1}}}),
    ]

    tools.append(types.Tool(name=GIT_STATUS, description="查看项目的 Git 分支和文件状态。entries 中的两位状态分别对应暂存区和工作树，?? 表示未跟踪。",
                            annotations=hints, inputSchema={"type": "object", "properties": {}, "additionalProperties": False}))
    if files.allow_save:
        tools.append(types.Tool(name=SAVE, description="用完整 UTF-8 文本替换已读取的现有文件。必须使用 read_workspace_text 返回的 revision，过期时重新读取。",
                                annotations=types.ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=False, openWorldHint=False),
                                inputSchema={"type": "object", "additionalProperties": False, "required": ["path","revision","content"],
                                             "properties": {"path": {**path, "minLength": 1}, "content": {"type": "string", "maxLength": 131072},
                                                            "revision": {"type": "string", "pattern": "^[0-9a-f]{64}$"}}}))

    @server.list_tools()
    async def list_tools():
        return tools

    @server.call_tool()
    async def call_tool(name, arguments):
        try:
            data = await files.call(name, arguments)
        except BridgeError as exc:
            # Provider responses, stderr and credentials are kept out of the protocol.
            code = exc.code
            if exc.code in {"machine_not_running", "machine_suspended"}:
                message = "云机当前未运行，请先在 InstaBox 启动或恢复实例。"
            elif exc.code in {"not_found", "io_error", "invalid_cursor", "invalid_path", "too_large",
                              "unsupported_file", "network", "missing_key", "credential_store", "invalid_response",
                              "conflict", "read_required", "git_error"}:
                message = str(exc)
            else:
                code = "cloud_error"
                message = "无法访问云端工作区，请检查机器、项目、网络和凭据权限。"
            return types.CallToolResult(isError=True, structuredContent={"code": code}, content=[types.TextContent(type="text", text=message)])
        # JSON text is also readable by clients that predate structured tool results.
        # Escaped JSON preserves POSIX filenames containing undecodable bytes.
        text = json_bytes(data).decode("utf-8")
        return [types.TextContent(type="text", text=text)]

    async with stdio_server() as (reader, writer):
        await server.run(reader, writer, server.create_initialization_options())


def main():
    parser = argparse.ArgumentParser(description="InstaBox 项目 MCP 接口（默认只读）")
    parser.add_argument("--machine", required=True, help="已有 Linux 云机的标识")
    parser.add_argument("--project", required=True, help="云机上已有项目的绝对目录")
    parser.add_argument("--credential-source", choices=("system", "environment"), default="system")
    parser.add_argument("--allow-save", action="store_true", help="允许保存本会话已读取的现有文本文件")
    parser.add_argument("--host", choices=("claude", "codex"), default="claude", help="选择配置片段格式")
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--store-key", action="store_true", help="交互式将密钥保存到当前用户的系统凭据库")
    action.add_argument("--print-config", action="store_true", help="打印所选官方客户端的配置片段")
    args = parser.parse_args()
    project = PurePosixPath(args.project)
    if not re.fullmatch(r"m_[A-Za-z0-9_]+", args.machine):
        parser.error("请输入有效的 InstaBox 机器标识。")
    if not project.is_absolute() or project.parent == project or ".." in project.parts or "\x00" in args.project:
        parser.error("请指定已有项目的绝对目录。")
    if args.print_config:
        child_args = [str(Path(__file__).resolve()), "--machine", args.machine, "--project", str(project),
                      "--credential-source", args.credential_source]
        if args.allow_save:
            child_args.append("--allow-save")
        config = {"command": sys.executable, "args": child_args}
        if args.host == "codex":
            # JSON strings and arrays of strings share TOML's basic string syntax.
            # Preserve non-BMP characters as UTF-8, not escaped surrogate pairs.
            print("[mcp_servers.instabox-workspace]")
            for name, value in config.items():
                print(name + " = " + json.dumps(value, ensure_ascii=False).replace(chr(127), r"\u007f"))
            if args.allow_save:
                print('tools.save_workspace_text.approval_mode = "prompt"')
            # Codex filters the environment of stdio servers. urllib/OpenSSL need
            # the host's HTTPS proxy and CA settings on managed networks.
            inherit = ["HTTPS_PROXY", "https_proxy", "NO_PROXY", "no_proxy", "SSL_CERT_FILE", "SSL_CERT_DIR"]
            if args.credential_source == "environment":
                inherit.append("INSTABOX_API_KEY")
            elif sys.platform.startswith("linux"):
                inherit.extend(("DBUS_SESSION_BUS_ADDRESS", "XDG_RUNTIME_DIR", "CLOUDWORKSPACE_NESTED_GNOME"))
            print("env_vars = " + json.dumps(inherit))
        else:
            print(json.dumps({"mcpServers": {"instabox-workspace": config}}, ensure_ascii=True, indent=2))
        return 0
    if args.store_key:
        if args.credential_source != "system" or not sys.stdin.isatty():
            parser.error("保存系统凭据需要交互式终端和 --credential-source system。")
        try:
            vault = system_vault()
            with warnings.catch_warnings():
                warnings.simplefilter("error", getpass.GetPassWarning)
                key = getpass.getpass("InstaBox API key（不回显）: ").strip()
            if not key:
                parser.error("密钥不能为空。")
            vault.set_password(VAULT_SERVICE, args.machine, key)
        except Exception:
            parser.exit(1, vault_help() + "\n")
        return 0
    try:
        asyncio.run(serve(CloudFiles(args.machine, str(project), args.credential_source, allow_save=args.allow_save)))
    except ImportError:
        parser.exit(1, "缺少 MCP 组件，请用此 Python 安装 requirements-mcp.txt。\n")
    except KeyboardInterrupt:
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

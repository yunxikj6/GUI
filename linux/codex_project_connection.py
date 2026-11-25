"""Register the read-only stdio adapter in ChatGPT Desktop's local Codex settings."""
import fcntl
import json
import os
from pathlib import Path
import select
import subprocess
import time

from workspace_mcp import is_previous_readonly_server

BUNDLED_CODEX = Path("/usr/lib/chatgpt/resources/codex")
NAME = "instabox-workspace"
_TIMEOUT = 12
_LIMIT = 8 * 1024 * 1024


class _AppServer:
    def __init__(self, process):
        self.process = process
        self.sequence = 0
        self.buffer = bytearray()

    def send(self, message):
        self.process.stdin.write(json.dumps(message, ensure_ascii=True).encode("ascii") + b"\n")
        self.process.stdin.flush()

    def call(self, method, params):
        self.sequence += 1
        self.send({"id": self.sequence, "method": method, "params": params})
        deadline = time.monotonic() + _TIMEOUT
        while time.monotonic() < deadline:
            end = self.buffer.find(b"\n")
            if end > _LIMIT or (end < 0 and len(self.buffer) > _LIMIT):
                raise ValueError("ChatGPT 返回的本机配置过大。")
            if end < 0:
                pipe = self.process.stdout
                if not select.select([pipe], [], [], max(0, deadline - time.monotonic()))[0]:
                    break
                data = os.read(pipe.fileno(), 65536)
                if not data:
                    raise ValueError("ChatGPT 配置进程提前退出，请检查客户端版本及本机配置。")
                self.buffer.extend(data)
                continue
            response = json.loads(self.buffer[:end])
            del self.buffer[:end + 1]
            if response.get("id") != self.sequence:
                continue
            if "error" in response:
                details = response["error"].get("data") or {}
                if isinstance(details, dict) and details.get("config_write_error_code") == "configVersionConflict":
                    raise ValueError("ChatGPT 配置已被其他进程修改，请重新打开连接设置。")
                raise ValueError("ChatGPT 拒绝读取或保存配置，请检查本机设置和组织策略。")
            return response["result"]
        raise ValueError("等待 ChatGPT 本机配置接口超时，请稍后重试。")


def _state(call, config, server):
    result = call("config/read", {"includeLayers": True})
    user = None
    inherited = False
    for layer in result["layers"]:
        name = layer["name"]
        current = layer["config"].get("mcp_servers", {}).get(NAME)
        if name["type"] == "user" and not name.get("profile") and Path(name["file"]) == config:
            user = layer
        elif current is not None and not layer.get("disabledReason"):
            if current != server:
                raise ValueError("ChatGPT 的其他配置层已定义同名连接，请先检查 instabox-workspace。")
            inherited = True
    if user is None or not isinstance(user["version"], str) or not user["version"]:
        raise ValueError("无法确认 ChatGPT 当前用户的配置版本。")
    current = user["config"].get("mcp_servers", {}).get(NAME)
    effective = result["config"].get("mcp_servers", {}).get(NAME)
    if effective is not None and effective.get("enabled") is False:
        raise ValueError("ChatGPT 已停用此连接，请先在客户端检查设置。")
    if current == server or (current is None and inherited):
        if not effective or any(effective.get(k) != v for k, v in server.items()):
            raise ValueError("ChatGPT 的有效配置与已保存的连接不同，请检查其他配置层。")
        return user["version"], current, True
    if current is None and effective is not None:
        raise ValueError("无法确认同名 ChatGPT 连接的来源，请先检查 instabox-workspace。")
    return user["version"], current, False


def _update(call, config, server, previous):
    version, current, ready = _state(call, config, server)
    if ready:
        return False
    if current is not None and (not isinstance(current, dict) or set(current) != set(server) or
            (current != previous and not is_previous_readonly_server(current, server, host="codex"))):
        raise ValueError("ChatGPT 已有不同的 instabox-workspace 配置，已保留原连接。")
    # Edit only the changed values so comments on adjacent keys and tables survive.
    edits = [{"keyPath": f"mcp_servers.{NAME}.{key}", "value": value, "mergeStrategy": "upsert"}
             for key, value in server.items() if current is None or current.get(key) != value]
    result = call("config/batchWrite", {"filePath": str(config), "expectedVersion": version, "edits": edits})
    if result["status"] != "ok" or Path(result["filePath"]) != config:
        raise ValueError("ChatGPT 未确认此连接生效，请检查管理策略或较高优先级的配置。")
    if not _state(call, config, server)[2]:
        raise ValueError("ChatGPT 配置写入后已发生变化，请重新检查连接。")
    return True


def configure(server: dict, *, previous=None, executable=None) -> bool:
    """Return True after a verified write, or False when the same settings already apply."""
    if (not isinstance(server, dict) or not isinstance(server.get("command"), str) or
            not os.path.isabs(server["command"]) or not is_previous_readonly_server(server, server, host="codex")):
        raise ValueError("ChatGPT 需要此项目使用系统密钥环的标准只读连接配置。")
    binary = Path(executable or BUNDLED_CODEX)
    if not binary.is_absolute() or not binary.is_file() or not os.access(binary, os.X_OK):
        raise ValueError("找不到 ChatGPT 官方客户端的 Codex 配置程序，请先安装或更新 Linux 版 ChatGPT。")
    home = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")
    if not home.is_absolute():
        raise ValueError("CODEX_HOME 必须是绝对目录。")
    try:
        home = home.resolve()
        home.mkdir(mode=0o700, parents=True, exist_ok=True)
        directory = os.open(home, os.O_RDONLY | os.O_DIRECTORY)
        try:
            # The native version check alone does not serialize independent app-server processes.
            deadline = time.monotonic() + _TIMEOUT
            while True:
                try:
                    fcntl.flock(directory, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise ValueError("另一项 ChatGPT 连接设置尚未结束，请稍后重试。") from None
                    time.sleep(0.05)
            env = dict(os.environ, CODEX_HOME=str(home))
            env.pop("INSTABOX_API_KEY", None)
            with subprocess.Popen([str(binary), "app-server"], cwd="/", env=env, stdin=subprocess.PIPE,
                                  stdout=subprocess.PIPE, stderr=subprocess.DEVNULL) as process:
                try:
                    rpc = _AppServer(process)
                    rpc.call("initialize", {"clientInfo": {"name": "cloudworkspace", "version": "1.0"}})
                    rpc.send({"method": "initialized"})
                    return _update(rpc.call, home / "config.toml", server, previous)
                finally:
                    process.stdin.close()
                    try:
                        process.wait(timeout=1)
                    except subprocess.TimeoutExpired:
                        process.kill()
        finally:
            os.close(directory)
    except (OSError, KeyError, TypeError, AttributeError, json.JSONDecodeError, UnicodeError):
        # The native protocol and stderr can contain unrelated MCP environment/header values.
        raise ValueError("无法完成 ChatGPT 本机配置操作，请检查文件权限、配置格式及客户端版本。") from None

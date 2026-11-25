"""Register the current InstaBox project with an official Ubuntu desktop client."""
from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import subprocess
import tempfile

from linux.native_file_picker import _display
from linux.native_project_setup import Zenity
from workspace_mcp import client_server_config


class _Dismissed(Exception):
    pass


class ConnectionDialogs(Zenity):
    def authorize(self, name, machine, project, install_runtime):
        target = _display(project).replace("\\", "\\\\")
        text = (f"客户端  {name}\n云机  {machine}\n项目  {target}\n\n"
                "当前 Ubuntu 用户的 instabox-workspace 连接将指向此目录，使用密钥环里的凭据。"
                "允许列出目录、读取文本和查询 Git 状态。"
                "调用工具时，返回的项目内容可进入厂商的会话。")
        if install_runtime:
            text += "\n\n首次使用会下载 Python 连接组件，安装到当前 Ubuntu 用户目录。"
        return self.run("连接 InstaBox 到官方客户端", [
            "--question", "--default-cancel", "--width=540", "--text=" + text,
            "--ok-label=连接此项目", "--cancel-label=取消",
        ]) is not None

    @contextmanager
    def installing(self):
        env = {k: v for k, v in os.environ.items() if k != "INSTABOX_API_KEY"}
        with subprocess.Popen(["zenity", "--progress", "--no-markup", "--pulsate", "--auto-close",
                               "--no-cancel", "--title=准备 InstaBox 连接", "--width=460",
                               "--text=正在准备当前用户的连接组件…"], env=env, text=True,
                              stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL) as dialog:
            def update(message):
                if dialog.poll() is not None:
                    raise _Dismissed
                try:
                    dialog.stdin.write("# " + message.replace("\n", " ") + "\n")
                    dialog.stdin.flush()
                except BrokenPipeError:
                    raise _Dismissed from None
            try:
                yield update
                if dialog.poll() is not None:
                    raise _Dismissed
            finally:
                try:
                    dialog.stdin.close()
                except BrokenPipeError:
                    pass

    def completed(self, name, changed, machine, project, help_text):
        status = "已保存连接" if changed else "此项目的连接已就绪"
        target = _display(project).replace("\\", "\\\\")
        self.run(name + " · InstaBox", [
            "--info", "--width=540", "--ok-label=完成",
            "--text=" + f"{status}\n{machine}   {target}\n\n{help_text}",
        ])


def register(host, server, state):
    if host == "codex":
        from linux.codex_project_connection import configure
    else:
        from linux.claude_project_connection import configure

    directory = Path(state) / "client-connections"
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    lock = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        # A receipt identifies the last entry created by this workspace even after
        # the user moves the unpacked archive. Never persist third-party config.
        fcntl.flock(lock, fcntl.LOCK_EX)
        receipt = directory / (host + ".json")
        if receipt.is_symlink():
            raise ValueError("连接记录是符号链接。请检查本机工作区的状态目录。")
        try:
            previous = json.loads(receipt.read_text(encoding="utf-8"))
        except (FileNotFoundError, UnicodeError, json.JSONDecodeError):
            previous = None
        previous = previous if isinstance(previous, dict) else None
        if previous == server:
            return configure(server, previous=previous)
        # Prepare the receipt before changing vendor settings. Publish it only after
        # the selected client accepts this entry.
        with tempfile.NamedTemporaryFile(dir=directory, mode="w", encoding="utf-8", delete=False) as stream:
            temporary = Path(stream.name)
            try:
                json.dump(server, stream, ensure_ascii=False)
                stream.flush()
                os.fsync(stream.fileno())
                changed = configure(server, previous=previous)
                try:
                    temporary.replace(receipt)
                except OSError as exc:
                    raise ValueError("客户端配置已保存。无法更新本机连接记录，请检查状态目录后重试。") from exc
                return changed
            finally:
                temporary.unlink(missing_ok=True)
    finally:
        os.close(lock)


def connect_project_client(*, load_project, state, dialogs=None):
    from linux.native_mcp_runtime import find_runtime, prepare_runtime

    ui = dialogs or ConnectionDialogs()
    choices = (("ChatGPT", "本机 Codex 项目"), ("Claude", "Claude Desktop 本地 MCP 服务器"))
    choice = ui.choose("连接 InstaBox 到官方客户端", "选择要使用云项目的 Ubuntu 客户端。",
                       ("官方客户端", "使用位置"), choices)
    if choice is None:
        return
    host = ("codex", "claude")[choice]
    if host == "codex" and not os.access("/usr/lib/chatgpt/resources/codex", os.X_OK):
        raise ValueError("请在 Ubuntu 的“安装官方客户端”中安装或更新 ChatGPT，再连接本地 Codex 项目。")
    settings = load_project()
    if settings is None:
        return
    machine, project, preferred = settings
    if not isinstance(preferred, str) or not os.path.isabs(preferred):
        preferred = None
    python = find_runtime(preferred)
    name = choices[choice][0]
    if not ui.authorize(name, machine, project, python is None):
        return
    if python is None:
        try:
            with ui.installing() as progress:
                python = prepare_runtime(preferred, progress=progress)
        except _Dismissed:
            return  # The downloaded runtime can be reused the next time the user connects.
    server = client_server_config(python, machine, project, host=host)
    changed = register(host, server, state)
    if host == "codex":
        help_text = "完全退出并重新打开 ChatGPT，在本机 Codex 项目的 MCP 设置中查看 instabox-workspace。"
    else:
        help_text = "完全退出并重新打开 Claude，在设置的 Developer → Local MCP servers 中查看 instabox-workspace。"
    ui.completed(name, changed, machine, project, help_text)

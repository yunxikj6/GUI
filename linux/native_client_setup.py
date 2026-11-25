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


_SCOPES = {"basic": "基础读取", "diff": "包含 Git 差异"}
_UNREAD = object()


class _Dismissed(Exception):
    pass


class ConnectionDialogs(Zenity):
    def authorize(self, name, machine, project, install_runtime, previous_scope):
        path = _display(project)
        # A Zenity list's message label clips very long paths. Keep its ends visible and
        # put the full path in the horizontally scrollable table in that case.
        long_path = len(path.encode("utf-8")) > 80
        target = (f"{path[:18]}…{path[-22:]}" if long_path else path).replace("\\", "\\\\")
        text = (f"客户端  {name}　　云机  {machine}\n项目  {target}\n\n"
                "当前用户的客户端将使用系统密钥环里的凭据连接此项目。\n"
                "基础读取允许列出目录、读取文本和查询 Git 状态。\n"
                "包含 Git 差异还可读取未暂存、已暂存和最近一次提交中的修改与删除行，\n"
                "以及未跟踪文件的文本。返回的项目内容可进入厂商会话。")
        if install_runtime:
            text += "\n首次使用会下载 Python 连接组件，安装到当前 Ubuntu 用户目录。"
        if previous_scope is not None:
            text += f"\n上次为此客户端和项目保存的权限为“{_SCOPES[previous_scope]}”。"
        selected = previous_scope or "basic"
        columns = ["--column=选择", "--column=值", "--column=读取权限"]
        path_cell = ""
        if long_path:
            columns.append("--column=完整项目目录")
            path_cell = path + "\n"
        cells = "".join(f"{'TRUE' if mode == selected else 'FALSE'}\n{mode}\n{label}\n{path_cell}"
                        for mode, label in _SCOPES.items())
        result = self.run("连接 InstaBox 到官方客户端", [
            "--list", "--radiolist", "--width=600", "--height=550", "--text=" + text,
            *columns, "--hide-column=2", "--print-column=2",
            "--ok-label=连接此项目", "--cancel-label=取消",
        ], cells=cells)
        if result is not None and result not in _SCOPES:
            raise ValueError("未能识别选中的读取权限，请重新打开连接设置。")
        return result

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


def _read_receipt(receipt):
    if receipt.is_symlink():
        raise ValueError("连接记录是符号链接。请检查本机工作区的状态目录。")
    try:
        previous = json.loads(receipt.read_text(encoding="utf-8"))
    except (FileNotFoundError, UnicodeError, json.JSONDecodeError):
        return None
    return previous if isinstance(previous, dict) else None


def _saved_scope(previous, machine, project):
    args = previous.get("args") if previous else None
    if isinstance(args, list):
        base = ["--machine", machine, "--project", str(project), "--credential-source", "system"]
        if args[1:] == base:
            return "basic"
        if args[1:] == base + ["--allow-diff"]:
            return "diff"
    return None


def register(host, server, state, *, expected=_UNREAD):
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
        previous = _read_receipt(receipt)
        if expected is not _UNREAD and previous != expected and previous != server:
            raise ValueError("客户端的项目连接已更改。请重新打开连接设置，确认最新的项目和读取权限。")
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
    previous = _read_receipt(Path(state) / "client-connections" / (host + ".json"))
    scope = ui.authorize(name, machine, project, python is None, _saved_scope(previous, machine, project))
    if scope is None:
        return
    if scope not in _SCOPES:
        raise ValueError("未能识别选中的读取权限。")
    if python is None:
        try:
            with ui.installing() as progress:
                python = prepare_runtime(preferred, progress=progress)
        except _Dismissed:
            return  # The downloaded runtime can be reused the next time the user connects.
    server = client_server_config(python, machine, project, host=host, allow_diff=scope == "diff")
    changed = register(host, server, state, expected=previous)
    if host == "codex":
        help_text = "完全退出并重新打开 ChatGPT，在本机 Codex 项目的 MCP 设置中查看 instabox-workspace。"
    else:
        help_text = "完全退出并重新打开 Claude，在设置的 Developer → Local MCP servers 中查看 instabox-workspace。"
    ui.completed(name, changed, machine, project, f"读取权限  {_SCOPES[scope]}\n\n{help_text}")

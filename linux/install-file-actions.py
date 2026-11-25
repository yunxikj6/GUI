#!/usr/bin/env python3
"""Install this user's InstaBox project launcher and native GNOME Files actions."""
import argparse
import hashlib
import os
from pathlib import Path
import stat
import sys
import tempfile


ACTIONS = {"保存到 InstaBox": "publish", "取回 InstaBox 最新版本": "refresh"}
ENTRY_ID = "cloudworkspace-instabox-project-files.desktop"
CLIENT_ENTRY_ID = "cloudworkspace-instabox-clients.desktop"
SCRIPT_HEADER = b"#!/usr/bin/python3\n# CloudLinuxWorkspace Nautilus action v1 sha256="
ENTRY_HEADER = b"[Desktop Entry]\n# CloudLinuxWorkspace local project launcher v1 sha256="
BODY = '''
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
from urllib.parse import urlsplit


def error(message):
    print(message, file=sys.stderr)
    title = TITLE
    commands = [
        ["notify-send", "--app-name=InstaBox", title, message],
        ["zenity", "--error", "--no-markup", "--title=" + title, "--text=" + message],
        ["gdbus", "call", "--session", "--dest", "org.freedesktop.Notifications",
         "--object-path", "/org/freedesktop/Notifications",
         "--method", "org.freedesktop.Notifications.Notify",
         "InstaBox", "0", "", title, message, "[]", "{}", "8000"],
    ]
    for args in commands:
        if shutil.which(args[0]):
            try:
                if subprocess.run(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0:
                    break
            except OSError:
                pass
    return 1


def main():
    # Nautilus percent-encodes newlines in its URIs. Only argv carries the local path.
    uris = os.environ.get("NAUTILUS_SCRIPT_SELECTED_URIS", "").splitlines()
    current = os.environ.get("NAUTILUS_SCRIPT_CURRENT_URI")
    try:
        for uri in uris or ([current] if current else []):
            parsed = urlsplit(uri)
            if parsed.scheme != "file" or parsed.netloc.lower() not in ("", "localhost"):
                return error("此操作仅支持本机文件。请在本机项目目录中选择文件。")
    except ValueError:
        return error("无法识别所选文件的位置。请在本机项目目录中重新选择文件。")
    if len(sys.argv) != 2 or len(uris) > 1:
        return error("请只选择一个本机普通文件。")
    path = Path(os.path.abspath(sys.argv[1]))
    try:
        if not stat.S_ISREG(path.lstat().st_mode):
            return error("请选择一个普通文件。此操作无法处理目录和符号链接。")
    except OSError:
        return error("无法读取选中的文件。请检查文件是否还在原位置。")
    if not Path(CLIENT).is_file():
        return error("找不到工作区程序。请在当前安装位置重新运行 integrate。")
    try:
        os.execv(PYTHON, [PYTHON, CLIENT, VERB, "--notify", "--", str(path)])
    except OSError:
        return error("无法启动工作区 Python。请在当前安装位置重新运行 integrate。")


sys.exit(main())
'''


def managed_bytes(header, source):
    body = source.encode("utf-8")
    return header + hashlib.sha256(body).hexdigest().encode("ascii") + b"\n" + body


def action_bytes(python, client, title, verb):
    # Store literals instead of shell commands. Preserve the venv symlink itself.
    settings = (f"PYTHON = {ascii(str(python))}\nCLIENT = {ascii(str(client))}\n"
                f"TITLE = {ascii(title)}\nVERB = {ascii(verb)}\n")
    return managed_bytes(SCRIPT_HEADER, settings + BODY)


def exec_argument(path):
    # XDG decodes string escapes before parsing quoted arguments and expanding field codes.
    value = "".join("\\" + ch if ch in '\\"$`' else ch for ch in str(path))
    value = value.replace("\\", "\\\\").replace("%", "%%")
    return '"' + value.replace("\n", "\\n").replace("\r", "\\r").replace("\t", "\\t") + '"'


def entry_bytes(python, client):
    command = f"{exec_argument(python)} {exec_argument(client)}"
    return managed_bytes(ENTRY_HEADER, (
        "Type=Application\nName=InstaBox Project Files\nName[zh_CN]=InstaBox 项目文件\n"
        f"Exec={command} browse --notify\n"
        "Icon=folder-remote\nTerminal=false\nStartupNotify=false\nCategories=Utility;FileTools;\n"
        "Actions=Configure;ConnectClients;\n\n[Desktop Action Configure]\nName=Connect to an InstaBox project\n"
        f"Name[zh_CN]=连接 InstaBox 项目\nExec={command} configure --notify\n"
        "\n[Desktop Action ConnectClients]\nName=Connect InstaBox to desktop apps\n"
        f"Name[zh_CN]=连接 InstaBox 到官方客户端\nExec={command} connect-clients --notify\n"
    ))


def client_entry_bytes(python, client):
    command = f"{exec_argument(python)} {exec_argument(client)}"
    return managed_bytes(ENTRY_HEADER, (
        "Type=Application\nName=Connect InstaBox to desktop apps\nName[zh_CN]=连接 InstaBox 到官方客户端\n"
        "Comment=Use an InstaBox project with Claude or a local ChatGPT Codex project\n"
        "Comment[zh_CN]=在 Claude 或 ChatGPT 的本地 Codex 项目中使用 InstaBox 文件\n"
        "Keywords=ChatGPT;Codex;Claude;InstaBox;MCP;\n"
        f"Exec={command} connect-clients --notify\n"
        "Icon=network-server\nTerminal=false\nStartupNotify=false\nCategories=Utility;\n"
    ))


def is_managed(content, header):
    if not content.startswith(header):
        return False
    digest, separator, body = content[len(header):].partition(b"\n")
    return bool(separator) and digest == hashlib.sha256(body).hexdigest().encode("ascii")


def inspect(path, expected, header, mode):
    """Return whether a write is needed and the identity of the entry being replaced."""
    try:
        info = path.lstat()
    except FileNotFoundError:
        return True, None
    if not stat.S_ISREG(info.st_mode) or info.st_size > 128 * 1024:
        raise ValueError(f"保留已有的同名文件，请先检查：{path}")
    current = path.read_bytes()
    if not is_managed(current, header):
        raise ValueError(f"保留已有或改写过的同名文件，请先检查：{path}")
    needs_write = current != expected or (mode == 0o700 and not info.st_mode & stat.S_IXUSR)
    return needs_write, info


def write_entry(path, expected, header, mode):
    needed, info = inspect(path, expected, header, mode)
    if not needed:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".instabox-", delete=False) as stream:
        temp = Path(stream.name)
        try:
            stream.write(expected)
            stream.flush()
            os.fchmod(stream.fileno(), mode)
            if info is None:
                # A new action must not replace a concurrent user's file.
                try:
                    os.link(temp, path)
                except FileExistsError:
                    if inspect(path, expected, header, mode)[0]:
                        raise ValueError(f"发现新建的同名文件，请先检查：{path}")
            else:
                now = path.lstat()
                if (not os.path.samestat(now, info) or now.st_ctime_ns != info.st_ctime_ns):
                    if not inspect(path, expected, header, mode)[0]:
                        return
                    raise ValueError(f"文件在注册期间已更改，请重试：{path}")
                temp.replace(path)
        finally:
            temp.unlink(missing_ok=True)


def install(python, client, *, entry_only=False):
    if not python.is_absolute() or not python.is_file() or not os.access(python, os.X_OK):
        raise ValueError("--python 需要指定可执行的 Python 绝对路径。")
    if not client.is_absolute() or not client.is_file():
        raise ValueError("--client 需要指定工作区程序的绝对路径。")
    base = Path(os.environ.get("XDG_DATA_HOME", ""))
    if not base.is_absolute():
        base = Path.home() / ".local" / "share"
    scripts = base / "nautilus" / "scripts"
    entries = [] if entry_only else [
        (scripts / title, action_bytes(python, client, title, verb), SCRIPT_HEADER, 0o700)
        for title, verb in ACTIONS.items()
    ]
    entries.append((base / "applications" / ENTRY_ID, entry_bytes(python, client), ENTRY_HEADER, 0o644))
    # Detect existing customizations before updating related entries.
    for entry in entries:
        inspect(*entry)
    for entry in entries:
        write_entry(*entry)
    # The same action remains reachable from Project Files if this optional search
    # entry collides with a desktop file previously registered by the user.
    try:
        write_entry(base / "applications" / CLIENT_ENTRY_ID, client_entry_bytes(python, client), ENTRY_HEADER, 0o644)
    except (OSError, ValueError) as exc:
        print(f"客户端的独立搜索入口未能更新。可从“InstaBox 项目文件”的菜单打开。{exc}", file=sys.stderr)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--python", required=True, type=Path)
    parser.add_argument("--client", required=True, type=Path)
    parser.add_argument("--entry-only", action="store_true")
    args = parser.parse_args()
    try:
        install(args.python, args.client, entry_only=args.entry_only)
    except (OSError, ValueError) as exc:
        print(exc, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""Open scoped InstaBox working copies using the local GNOME file manager."""
import argparse
from functools import lru_cache
import html
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import sys
import tempfile

from instabox import BridgeError, InstaBox
from native_file_copies import Copies
from workspace_mcp import read_secret

ROOT = Path(__file__).resolve().parent


def xdg_path(variable, fallback):
    value = Path(os.environ.get(variable) or "")
    return (value if value.is_absolute() else Path.home() / fallback) / "cloudworkspace"


def settings_path():
    return xdg_path("XDG_CONFIG_HOME", ".config") / "native-files.json"


def validate(machine, project):
    if not isinstance(machine, str) or not re.fullmatch(r"m_[A-Za-z0-9_]+", machine):
        raise ValueError("请提供 InstaBox 机器标识 m_…。")
    path = PurePosixPath(project)
    if path.anchor != "/" or path == path.parent or ".." in path.parts or "\0" in project:
        raise ValueError("请指定云机上已有项目的绝对目录。")
    return machine, str(path)


def load_settings():
    try:
        saved = json.loads(settings_path().read_text(encoding="utf-8"))
        machine, project = validate(saved["machine"], saved["project"])
        return machine, project, saved.get("python")
    except FileNotFoundError:
        raise ValueError("请先运行 native_files.py integrate 配置云项目。") from None
    except (TypeError, KeyError, UnicodeError, json.JSONDecodeError):
        raise ValueError("无法读取云项目配置，请重新运行 integrate。") from None


def install_actions(python, *, entry_only=False):
    command = [sys.executable, str(ROOT / "linux" / "install-file-actions.py"),
               "--python", python, "--client", str(ROOT / "native_files.py")]
    if entry_only:
        command.append("--entry-only")
    subprocess.run(command, check=True)


def integrate(machine, project):
    machine, project = validate(machine, project)
    config = settings_path()
    config.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if config.is_symlink():
        raise ValueError("云项目配置路径是符号链接，请先检查配置目录。")
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=config.parent, delete=False) as tmp:
        name = Path(tmp.name)
        try:
            json.dump({"machine": machine, "project": project, "python": sys.executable}, tmp)
            tmp.flush()
            os.fsync(tmp.fileno())
            install_actions(sys.executable)
            name.replace(config)
        finally:
            name.unlink(missing_ok=True)


def show_file(path):
    # GNOME Files accepts file URIs with arbitrary filesystem bytes via the standard D-Bus API.
    local = Path(path).absolute()
    # On Noble a cold desktop portal can take 25 seconds before Files serves this call.
    result = subprocess.run(["gdbus", "call", "--session", "--timeout", "45",
                             "--dest", "org.freedesktop.FileManager1", "--object-path", "/org/freedesktop/FileManager1",
                             "--method", "org.freedesktop.FileManager1.ShowItems", json.dumps([local.as_uri()]), ""],
                            capture_output=True)
    if result.returncode:
        raise ValueError(f"工作副本已就绪。请在 Ubuntu 文件管理器中打开 {path}")
    remember_shown_copy(local)


def remember_shown_copy(path):
    # The project environment may lack GI. Use the desktop's Python and settings.
    env = os.environ.copy()
    env.pop("INSTABOX_API_KEY", None)
    for key in ("XDG_DATA_HOME", "XDG_CONFIG_HOME"):
        if key in env and not os.path.isabs(env[key]):
            del env[key]  # GLib accepts relative values; the XDG specification requires absolute paths.
    try:
        subprocess.run(["/usr/bin/python3", "-I", str(ROOT / "linux" / "recent_file.py"), str(path)],
                       env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=3)
    except (OSError, subprocess.TimeoutExpired):
        pass  # Files already showed the copy, even if recent history is unavailable.


def message(body, *, error=False, title="InstaBox 项目文件"):
    try:
        if error and shutil.which("zenity"):
            dialog = subprocess.run(["zenity", "--error", "--no-markup", "--title=" + title,
                                     "--text=" + body], check=False)
            if dialog.returncode == 0:
                return
        escaped = html.escape(body)
        if shutil.which("notify-send"):
            result = subprocess.run(["notify-send", "--app-name=" + title, "--",
                                     title, escaped], timeout=5, check=False)
            if result.returncode == 0:
                return
        if shutil.which("gdbus"):
            subprocess.run(["gdbus", "call", "--session", "--timeout", "4", "--dest",
                            "org.freedesktop.Notifications", "--object-path", "/org/freedesktop/Notifications",
                            "--method", "org.freedesktop.Notifications.Notify", "--", "InstaBox", "0", "",
                            title, escaped, "[]", "{}", "8000"], capture_output=True)
    except (OSError, subprocess.SubprocessError):
        # A notification service can disappear after the file operation already succeeded.
        pass


def client(credential_source):
    @lru_cache(maxsize=4)
    def transport(machine, project):
        return InstaBox(read_secret(machine, credential_source), machine, project)

    def rpc(machine, project, request):
        return transport(machine, project).file_request(request)

    return rpc


def configure():
    from linux.native_project_setup import configure_project

    current = load_settings()[:2] if settings_path().exists() else None
    return configure_project(validate, integrate, current=current)


def project_for_client():
    if not settings_path().exists() and not configure():
        return None
    return load_settings()


def run(args):
    if args.action == "integrate":
        integrate(args.machine, args.project)
        print("已注册“InstaBox 项目文件”和文件管理器的保存、取回动作。")
        return
    if args.action == "relink":
        if settings_path().is_file():
            _, _, python = load_settings()
            install_actions(python if isinstance(python, str) and Path(python).is_absolute()
                            and os.access(python, os.X_OK) else sys.executable)
        elif args.offer_setup:
            install_actions(sys.executable, entry_only=True)
        return
    if args.action == "configure":
        configure()
        return
    if args.action == "connect-clients":
        from linux.native_client_setup import connect_project_client
        connect_project_client(load_project=project_for_client, state=xdg_path("XDG_STATE_HOME", ".local/state"))
        return
    if args.action == "browse" and not settings_path().exists() and not configure():
        return

    rpc = client(args.credential_source)
    copies = Copies(xdg_path("XDG_DATA_HOME", ".local/share") / "file-copies", rpc)
    if args.action in ("checkout", "browse"):
        machine, project, _ = load_settings()
        if args.action == "browse":
            from linux.native_file_picker import choose_file
            relative = choose_file(rpc, machine, project)
            if relative is None:
                return
        else:
            relative = args.path
        local = copies.checkout(machine, project, relative)
        print(local, flush=True)
        if args.action == "browse" or args.show:
            show_file(local)
        return

    result = getattr(copies, args.action)(Path(args.path))
    print(json.dumps(result, ensure_ascii=True), flush=True)
    if args.notify:
        if result.get("remoteCopy"):
            show_file(result["remoteCopy"])
        labels = {"published": "已保存到云机。", "already_published": "已核对云机上的保存结果。",
                  "unchanged": "当前副本没有新修改。", "current": "当前副本与云机版本一致。",
                  "local_changes": "云端版本未变，本机仍有待发布的修改。",
                  "remote_copy": "已打开该云端版本的工作副本，原稿仍在原处。",
                  "restored": "已从云机恢复删除的本地副本。",
                  "clean": "本地文件与上次确认的云端版本一致。", "modified": "本机有待发布的修改。"}
        note = labels.get(result["status"], result["status"])
        if args.action == "publish" and result["dirty"]:
            note += " 本机另有更新的修改尚待发布。"
        if result.get("remoteCopyDirty"):
            note += " 打开的副本保留了上次取回后所做的本地修改。"
        message(note)


def main(argv=None):
    parser = argparse.ArgumentParser(description="在本机 GNOME 中打开和保存 InstaBox 项目工作副本。")
    parser.add_argument("--credential-source", choices=("system", "environment"), default="system")
    sub = parser.add_subparsers(dest="action", required=True)
    config = sub.add_parser("integrate", help="为当前 Ubuntu 用户连接云项目并注册原生入口")
    config.add_argument("--machine", required=True)
    config.add_argument("--project", required=True)
    relink = sub.add_parser("relink", help="更新桌面入口路径")
    relink.add_argument("--offer-setup", action="store_true", help="未连接项目时在活动菜单中提供首次设置入口")
    for name in ("configure", "connect-clients", "browse", "checkout", "publish", "refresh", "inspect"):
        cmd = sub.add_parser(name)
        cmd.add_argument("--notify", action="store_true", help="在当前 GNOME 会话显示结果")
        if name not in ("configure", "connect-clients", "browse"):
            cmd.add_argument("path", help="云端相对路径" if name == "checkout" else "登记的本地工作副本路径")
        if name == "checkout":
            cmd.add_argument("--show", action="store_true", help="在本机文件管理器中选中副本")
    args = parser.parse_args(argv)
    try:
        run(args)
    except (BridgeError, ValueError, RuntimeError, OSError, subprocess.SubprocessError) as exc:
        note = str(exc)
        if getattr(exc, "code", None) == "conflict":
            note += " 请选中原稿，使用“取回 InstaBox 最新版本”，在新副本中合并修改后再保存到云机。"
        print(note, file=sys.stderr)
        if getattr(args, "notify", False):
            title = "连接 InstaBox 到官方客户端" if args.action == "connect-clients" else "InstaBox 项目文件"
            message(note, error=True, title=title)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

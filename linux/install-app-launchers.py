#!/usr/bin/python3
"""Install this user's GNOME application entries, or activate the Windows app selected in GNOME."""
import argparse
from concurrent.futures import ThreadPoolExecutor
import configparser
import fcntl
import os
import platform
from pathlib import Path
import shutil
import subprocess
import struct
import sys
import tempfile
import time

SCRIPT = Path(__file__).resolve()
WINDOWS_DIR = SCRIPT.parent.parent / "windows"
WINDOWS_HELPER = WINDOWS_DIR / "Open-OfficialApp.ps1"
# Desktop IDs and commands shipped in the official Linux packages.
NATIVE_APPS = {
    "Claude": ("com.anthropic.Claude.desktop", "claude-desktop"),
    "ChatGPT": ("chatgpt.desktop", "chatgpt"),
}
INSTALLER_MARKER = "X-CloudWorkspace-Installer=true"
APP_IDS = {
    "ChatGPT": "cloudworkspace-windows-chatgpt.desktop",
    "Claude": "cloudworkspace-windows-claude.desktop",
}
MANAGED = "X-CloudWorkspace-WindowsLauncher=true"
GENERIC_ICON = "application-x-executable"
ICON_MAX_AGE = 24 * 60 * 60
MISSING_ICON_MAX_AGE = 5 * 60


def data_home():
    directory = Path(os.environ.get("XDG_DATA_HOME", ""))
    return directory if directory.is_absolute() else Path.home() / ".local" / "share"


def data_roots():
    dirs = os.environ.get("XDG_DATA_DIRS") or "/usr/local/share:/usr/share"
    return [data_home(), *(Path(item) for item in dirs.split(":") if item.startswith("/"))]


def application_entry(app_id, *, command=None):
    for base in data_roots():
        path = base / "applications" / app_id
        config = configparser.ConfigParser(interpolation=None, strict=False)
        try:
            with path.open(encoding="utf-8-sig") as reader:
                config.read_file(reader)
            entry = config["Desktop Entry"]
            if entry.get("Type") != "Application" or entry.getboolean("Hidden", False) or entry.getboolean("NoDisplay", False):
                return None
            if command:
                system_path = os.environ.get("CLOUDWORKSPACE_SYSTEM_PATH") if os.environ.get("CLOUDWORKSPACE_NESTED_GNOME") else None
                if entry.get("Exec", "").split()[:1] != [command] or not shutil.which(command, path=system_path):
                    return None
            elif not entry.getboolean("X-CloudWorkspace-WindowsLauncher", False):
                return None
            icon = entry.get("Icon", "")
            if not icon or any(c in icon for c in "\r\n\0"):
                icon = GENERIC_ICON
            return path, icon
        except FileNotFoundError:
            continue
        except (OSError, KeyError, ValueError, UnicodeError, configparser.Error):
            return None
    return None


def native_entry(app):
    app_id, command = NATIVE_APPS[app]
    return application_entry(app_id, command=command)


def exec_path_argument(path):
    # Desktop Entry string escapes are processed before the quoted Exec argument is decoded.
    value = str(path)
    if any(c in value for c in "\n\r\0"):
        raise ValueError("仓库路径含有换行符或空字符，无法生成桌面入口。")
    value = "".join("\\" + char if char in '\\"$`' else char for char in value)
    return '"' + value.replace("\\", "\\\\").replace("%", "%%") + '"'


def entry_text(app, icon):
    return "\n".join((
        "[Desktop Entry]",
        "Type=Application",
        f"Name={app} (Windows)",
        f"Name[zh_CN]={app}（Windows 版）",
        f"Comment=Open the official {app} app in its own Windows window",
        f"Comment[zh_CN]=在独立的 Windows 窗口中打开官方 {app} 应用",
        f"Exec=/usr/bin/python3 {exec_path_argument(SCRIPT)} --open {app}",
        "Icon=" + icon.replace("\\", "\\\\"),
        "Terminal=false",
        "StartupNotify=false",
        "Categories=Network;",
        MANAGED,
        "",
    ))


def update_entry(path, new_text, *, marker=MANAGED):
    if path.is_symlink():
        raise RuntimeError(f"已有同名符号链接，请检查后重试：{path}")
    try:
        prior = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        prior = None
    if prior == new_text:
        return
    if prior is not None and marker not in prior.splitlines():
        raise RuntimeError(f"已有其他程序创建的同名桌面项：{path}")
    if new_text is None:
        path.unlink(missing_ok=True)
        return
    # Readers see a complete desktop entry even if two startup sessions install at once.
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as output:
        tmp = Path(output.name)
        try:
            output.write(new_text)
            output.flush()
            os.chmod(tmp, 0o644)
            tmp.replace(path)
        finally:
            tmp.unlink(missing_ok=True)


def offer_app_setup():
    if not SCRIPT.with_name("setup-vendor-apps.sh").is_file():
        return
    applications = data_home() / "applications"
    applications.mkdir(parents=True, exist_ok=True)
    entry = "\n".join((
        "[Desktop Entry]", "Type=Application",
        "Name=Install official desktop apps", "Name[zh_CN]=安装官方客户端",
        "Comment=Install ChatGPT or Claude for Ubuntu",
        "Comment[zh_CN]=在 Ubuntu 内安装 ChatGPT 或 Claude",
        "Keywords=ChatGPT;Claude;install;", "Keywords[zh_CN]=ChatGPT;Claude;安装;",
        "Icon=system-software-install", "Categories=Settings;", "Terminal=true",
        f"Exec=/usr/bin/python3 {exec_path_argument(SCRIPT)} --setup-apps",
        INSTALLER_MARKER, "",
    ))
    update_entry(applications / "cloudworkspace-install-apps.desktop", entry, marker=INSTALLER_MARKER)


def setup_apps():
    env = os.environ.copy()
    env.pop("INSTABOX_API_KEY", None)
    result = subprocess.run(["/bin/bash", str(SCRIPT.with_name("setup-vendor-apps.sh"))], env=env)
    if result.returncode:
        if result.returncode != 130 and sys.stdin.isatty():
            try:
                input("安装未完成。请查看上方的软件包管理器消息，按 Enter 关闭。")
            except EOFError:
                pass
        return result.returncode
    if os.environ.get("WSL_DISTRO_NAME"):
        install()
    return subprocess.run([sys.executable, str(SCRIPT.with_name("desktop-shortcuts.py"))], env=env).returncode


def recent(path, seconds):
    try:
        return not path.is_symlink() and 0 <= time.time() - path.stat().st_mtime < seconds
    except OSError:
        return False


def valid_png(path):
    try:
        if path.is_symlink() or not path.is_file() or not 45 <= path.stat().st_size <= 2 * 1024 * 1024:
            return False
        with path.open("rb") as stream:
            header = stream.read(24)
            stream.seek(-12, os.SEEK_END)
            return (len(header) == 24 and header[:16] == b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR"
                    and all(0 < size <= 2048 for size in struct.unpack(">II", header[16:]))
                    and stream.read(12) == b"\x00\x00\x00\x00IEND\xaeB\x60\x82")
    except (OSError, ValueError):
        return False


def get_registered_icon(app, target):
    if not WINDOWS_HELPER.is_file():
        return False
    try:
        wslpath, powershell = windows_tools()
        result = subprocess.run([powershell, "-NoLogo", "-NoProfile", "-NonInteractive", "-File",
                                 windows_path(wslpath, WINDOWS_HELPER), "-App", app,
                                 "-IconPath", windows_path(wslpath, target)],
                                capture_output=True, timeout=12)
        return result.returncode == 0 and valid_png(target)
    except (OSError, RuntimeError, UnicodeError, ValueError, subprocess.TimeoutExpired):
        return False


def installed_icon(app):
    directory = data_home() / "cloudworkspace" / "windows-app-icons"
    icon = directory / (app.lower() + ".png")
    missing = icon.with_suffix(".unavailable")
    try:
        directory.mkdir(parents=True, exist_ok=True)
        # Two Start.cmd sessions can arrive together. Check freshness after taking the per-app lock.
        with icon.with_suffix(".lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            if recent(missing, MISSING_ICON_MAX_AGE):
                return GENERIC_ICON
            if recent(icon, ICON_MAX_AGE) and valid_png(icon):
                return str(icon)
            with tempfile.TemporaryDirectory(prefix=".scan-", dir=directory) as scratch:
                target = Path(scratch) / "icon.png"
                if get_registered_icon(app, target):
                    target.replace(icon)
                    missing.unlink(missing_ok=True)
                    return str(icon)
                # Leave a last-known PNG intact for existing shortcuts that reference it.
                marker = Path(scratch) / "unavailable"
                marker.touch()
                marker.replace(missing)
    except OSError:
        pass
    return GENERIC_ICON


def install():
    directory = data_home() / "applications"
    directory.mkdir(parents=True, exist_ok=True)
    apps = [app for app in APP_IDS if not native_entry(app)]
    icons = {}
    if apps:
        native_launcher()
        with ThreadPoolExecutor(max_workers=len(apps)) as pool:
            icons = dict(zip(apps, pool.map(installed_icon, apps)))
    for app, filename in APP_IDS.items():
        update_entry(directory / filename, entry_text(app, icons[app]) if app in icons else None)


def windows_path(wslpath, linux_path):
    result = subprocess.run([wslpath, "-w", str(linux_path)], capture_output=True, timeout=3)
    if result.returncode != 0:
        raise RuntimeError("WSL 无法转换启动脚本路径：" + result.stderr.decode("utf-8", "replace").strip())
    path = result.stdout.decode("utf-8").rstrip("\r\n")
    if not path:
        raise RuntimeError("wslpath 没有返回 Windows 启动脚本路径。")
    return path


def windows_tools():
    wslpath = shutil.which("wslpath")
    if not wslpath:
        raise RuntimeError("此入口需要在 Windows 的 WSL Ubuntu 中运行。当前找不到 wslpath。")
    powershell = shutil.which("powershell.exe")
    if not powershell:
        # appendWindowsPath may be disabled on an otherwise working WSL installation.
        locate = subprocess.run([wslpath, "-u", r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe"],
                                capture_output=True, timeout=3)
        candidate = os.fsdecode(locate.stdout).rstrip("\r\n")
        if locate.returncode == 0 and os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            powershell = candidate
    if not powershell:
        raise RuntimeError("找不到 Windows PowerShell。请确认 WSL 已启用 Windows 互操作，并可执行 powershell.exe。")
    return wslpath, powershell


def native_launcher():
    machine = platform.machine().lower()
    arch = {"x86_64": "x64", "amd64": "x64", "arm64": "arm64", "aarch64": "arm64"}.get(machine)
    if not arch:
        raise RuntimeError(f"当前系统架构没有可用的 Windows 启动器（{machine}）。")
    path = WINDOWS_DIR / f"CloudLinuxWorkspace-{arch}.exe"
    if not path.is_file():
        raise RuntimeError(f"找不到 Windows 启动器 {path.name}。请完整解压应用，再运行 Start.cmd 更新桌面入口。")
    return path


def activate(app):
    path = native_launcher()
    try:
        # WSL interop activates the PE directly. The Windows program displays its own app errors.
        return subprocess.run([str(path), "--open-app", app, "--offer-install"]).returncode
    except OSError as error:
        raise RuntimeError(f"WSL 无法执行 {path.name}。请确认已启用 Windows 互操作且文件可执行。\n{error}") from error


def show_error(app, message):
    print(message, file=sys.stderr)
    if not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
        return
    title = f"无法打开 {app}（Windows 版）"
    zenity = shutil.which("zenity")
    if zenity:
        subprocess.run([zenity, "--error", "--no-markup", "--title=" + title, "--text=" + message])
        return
    try:
        import gi
        gi.require_version("Gtk", "3.0")
        from gi.repository import Gtk
        if Gtk.init_check()[0]:
            dialog = Gtk.MessageDialog(message_type=Gtk.MessageType.ERROR, buttons=Gtk.ButtonsType.CLOSE, text=title)
            dialog.format_secondary_text(message)
            dialog.run()
            dialog.destroy()
            return
    except (ValueError, ImportError):
        pass
    notify = shutil.which("notify-send")
    if notify:
        import html
        subprocess.run([notify, title, html.escape(message)])


def main():
    cli = argparse.ArgumentParser(description=__doc__)
    action = cli.add_mutually_exclusive_group()
    action.add_argument("--open", choices=APP_IDS, metavar="APP", help="Activate an official Windows app.")
    action.add_argument("--setup-apps", action="store_true", help="Install native Ubuntu clients in a terminal.")
    args = cli.parse_args()
    try:
        if args.open:
            return activate(args.open)
        if args.setup_apps:
            return setup_apps()
        else:
            install()
    except (OSError, RuntimeError, UnicodeError, ValueError) as err:
        if args.open:
            show_error(args.open, str(err))
        else:
            print(err, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

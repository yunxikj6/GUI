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
INSTALL_IDS = {app: f"cloudworkspace-install-{app.lower()}.desktop" for app in NATIVE_APPS}
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
                argv = entry.get("Exec", "").split()
                if (argv[:1] != [command] or (base == data_home() and argv != [command, "%U"])
                        or not shutil.which(command, path=system_path)):
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


def has_native_registration(app):
    app_id, _ = NATIVE_APPS[app]
    return any(os.path.lexists(base / "applications" / app_id) for base in data_roots())


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
            if prior is None:
                # A concurrently created custom entry must survive first registration.
                try:
                    os.link(tmp, path)
                except FileExistsError:
                    if path.is_symlink() or path.read_bytes() != new_text.encode("utf-8"):
                        raise RuntimeError(f"桌面项同时发生了更改，请确认后重试：{path}") from None
            else:
                tmp.replace(path)
        finally:
            tmp.unlink(missing_ok=True)


def app_setup_text(app, hidden):
    return "\n".join((
        "[Desktop Entry]", "Type=Application", f"Name=Install {app} for Ubuntu", f"Name[zh_CN]=安装 Ubuntu 版 {app}",
        f"Comment=Install and open the official {app} app in Ubuntu",
        f"Comment[zh_CN]=安装并打开 Ubuntu 版官方 {app} 客户端",
        "Icon=system-software-install", "Categories=Settings;", "Terminal=true",
        f"NoDisplay={'true' if hidden else 'false'}",
        f"Exec=/usr/bin/python3 {exec_path_argument(SCRIPT)} --setup-app {app}", INSTALLER_MARKER, "",
    ))


def installer_entry(app, *, include_hidden=False):
    path = data_home() / "applications" / INSTALL_IDS[app]
    if path.is_symlink() or not path.is_file() or not SCRIPT.with_name("setup-vendor-apps.sh").is_file():
        return None
    try:
        entry = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return None
    if entry == app_setup_text(app, False) or (include_hidden and entry == app_setup_text(app, True)):
        return path, "system-software-install"
    return None


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
    entries = {"cloudworkspace-install-apps.desktop": entry}
    entries.update((filename, app_setup_text(app, has_native_registration(app))) for app, filename in INSTALL_IDS.items())
    errors = []
    for filename, text in entries.items():
        try:
            update_entry(applications / filename, text, marker=INSTALLER_MARKER)
        except (OSError, RuntimeError, UnicodeError) as error:
            errors.append(str(error))
    if errors:
        raise RuntimeError("\n".join(errors))


def open_registered_app(app, env):
    native = native_entry(app)
    if native:
        # The setup terminal closes as soon as this command finishes. Give GIO
        # and the client a session and standard streams independent of that PTY.
        result = subprocess.run(["/usr/bin/gio", "launch", str(native[0])], env=env,
                                start_new_session=True, stdin=subprocess.DEVNULL,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if result.returncode:
            raise RuntimeError(f"Ubuntu 未能通过 {native[0].name} 打开 {app}。请在“活动”中检查该应用后重试。")
        return True
    if has_native_registration(app):
        raise RuntimeError(f"Ubuntu 已有 {NATIVE_APPS[app][0]}。请检查该桌面项是否被隐藏、改写，及其官方启动命令是否可用。")
    return False


def refresh_after_setup(app, env):
    if os.environ.get("WSL_DISTRO_NAME"):
        if app:
            try:
                update_entry(data_home() / "applications" / APP_IDS[app], None)
            except (OSError, RuntimeError, UnicodeError) as error:
                print(f"Windows 菜单项未更新：{error}", file=sys.stderr)
        else:
            install()
    return subprocess.run([sys.executable, str(SCRIPT.with_name("desktop-shortcuts.py"))], env=env).returncode


def setup_apps(app=None):
    env = os.environ.copy()
    env.pop("INSTABOX_API_KEY", None)
    if app and open_registered_app(app, env):
        return refresh_after_setup(app, env)
    installer = SCRIPT.with_name("setup-vendor-apps.sh")
    # Both old and new unpack locations use this user's same installation lock.
    state = Path(os.environ.get("XDG_STATE_HOME", ""))
    directory = (state if state.is_absolute() else Path.home() / ".local/state") / "cloudworkspace/vendor-install"
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    lock = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError("另一个 Ubuntu 客户端安装窗口正在运行。请先完成或取消该安装，然后重试。") from None
        if app and open_registered_app(app, env):
            return refresh_after_setup(app, env)
        options = ["--app", app.lower()] if app else []
        result = subprocess.run(["/bin/bash", str(installer), *options], env=env)
    finally:
        os.close(lock)
    if result.returncode:
        if result.returncode != 130 and sys.stdin.isatty():
            try:
                input("安装未完成。请查看上方的软件包管理器消息，按 Enter 关闭。")
            except EOFError:
                pass
        return result.returncode
    if app and not open_registered_app(app, env):
        raise RuntimeError(f"安装程序已退出，Ubuntu 尚未注册官方 {app} 桌面项。请检查安装结果后重试。")
    return refresh_after_setup(app, env)


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
        update_entry(directory / filename, entry_text(app, icons[app]) if app in icons and not native_entry(app) else None)


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
    action.add_argument("--setup-app", choices=NATIVE_APPS, metavar="APP", help="Install and open the selected Ubuntu app.")
    args = cli.parse_args()
    try:
        if args.open:
            return activate(args.open)
        if args.setup_apps or args.setup_app:
            return setup_apps(args.setup_app)
        else:
            install()
    except (OSError, RuntimeError, UnicodeError, ValueError) as err:
        if args.open:
            show_error(args.open, str(err))
        else:
            print(err, file=sys.stderr)
            if args.setup_app and sys.stdin.isatty():
                try:
                    input("按 Enter 关闭。")
                except EOFError:
                    pass
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

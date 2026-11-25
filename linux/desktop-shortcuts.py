#!/usr/bin/python3
"""Offer each installed application a desktop shortcut once, in the GNOME user's session."""
import configparser
import ctypes
import fcntl
import importlib
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile

launchers = importlib.import_module("install-app-launchers")
APPS = tuple(launchers.APP_IDS)


def xdg_home(variable, fallback):
    value = Path(os.environ.get(variable, ""))
    return value if value.is_absolute() else Path.home() / fallback


def desktop_dir():
    glib = ctypes.CDLL("libglib-2.0.so.0")
    glib.g_get_user_special_dir.argtypes = [ctypes.c_int]
    glib.g_get_user_special_dir.restype = ctypes.c_char_p
    path = glib.g_get_user_special_dir(0)  # G_USER_DIRECTORY_DESKTOP
    return Path(os.fsdecode(path)) if path else None


def entry_for(app):
    roots = [launchers.data_home(), *(Path(p) for p in
             (os.environ.get("XDG_DATA_DIRS") or "/usr/local/share:/usr/share").split(":") if p.startswith("/"))]
    ids = [launchers.APP_IDS[app]]
    if app == "Claude":
        ids.insert(0, launchers.NATIVE_CLAUDE_ID)
    for app_id in ids:
        for base in roots:
            source = base / "applications" / app_id
            if not source.is_file():
                continue
            config = configparser.ConfigParser(interpolation=None, strict=False)
            try:
                config.read(source, encoding="utf-8-sig")
                entry = config["Desktop Entry"]
                if entry.get("Type") != "Application" or entry.getboolean("Hidden", False) or entry.getboolean("NoDisplay", False):
                    break
                if app_id == launchers.NATIVE_CLAUDE_ID:
                    if entry.get("Exec", "").split()[:1] != ["claude-desktop"] or not shutil.which("claude-desktop"):
                        break
                elif not entry.getboolean("X-CloudWorkspace-WindowsLauncher", False):
                    break
                icon = entry.get("Icon", "")
                if not icon or any(ch in icon for ch in "\r\n\0"):
                    icon = "application-x-executable"
                return source, icon
            except (KeyError, ValueError, OSError, configparser.Error):
                break
    return None


def stable_entry(app, source, marker):
    directory = launchers.data_home() / "cloudworkspace/desktop-shortcuts"
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    alias = directory / (app + ".desktop")
    try:
        alias.symlink_to(source)
    except FileExistsError:
        if not alias.is_symlink():
            raise RuntimeError(f"已存在同名的桌面入口文件，请检查 {alias}")
        previous = alias.readlink()
        if previous == source:
            return alias
        # Only retarget a link that this installer recorded creating.
        if not marker.is_file() or marker.read_text(encoding="utf-8") != "installed\n":
            raise RuntimeError(f"已存在其他桌面入口链接，请检查 {alias}")
        with tempfile.TemporaryDirectory(dir=directory) as scratch:
            new = Path(scratch) / alias.name
            new.symlink_to(source)
            new.replace(alias)
    return alias


def writable_desktop(state):
    desktop = desktop_dir()
    if desktop is None or not desktop.is_absolute() or desktop.resolve() == Path.home().resolve():
        return None
    if not desktop.exists():
        settings = xdg_home("XDG_CONFIG_HOME", ".config") / "user-dirs.dirs"
        if settings.exists() or desktop != Path.home() / "Desktop" or any((state / app).exists() for app in APPS):
            return None
        desktop.mkdir(mode=0o700, exist_ok=True)
    if not desktop.is_dir() or desktop.stat().st_mode & stat.S_IWOTH:
        return None  # DING disables trusted launchers in a world-writable desktop.
    return desktop


def ensure_shortcuts():
    state = xdg_home("XDG_STATE_HOME", ".local/state") / "cloudworkspace/desktop-shortcuts"
    available = {app: info for app in APPS if (info := entry_for(app))}
    if not available:
        return
    desktop = writable_desktop(state)
    if desktop is None and not state.is_dir():
        return
    gio = shutil.which("gio")
    if not gio:
        raise RuntimeError("需要 GNOME 的 gio 工具来设置桌面图标的启动权限。")
    state.mkdir(mode=0o700, parents=True, exist_ok=True)
    lock = os.open(state, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        fcntl.flock(lock, fcntl.LOCK_EX)
        for app, (source, icon) in available.items():
            marker = state / app
            if marker.exists():
                if marker.read_text(encoding="utf-8") == "installed\n":
                    stable_entry(app, source, marker)
                continue
            if desktop is None:
                continue
            target = desktop / (app + ".desktop")
            # Preserve a vendor shortcut or any file, directory or symlink the user already placed here.
            installed = False
            if not any(os.path.lexists(path) for path in (desktop / source.name, target)):
                alias = stable_entry(app, source, marker)
                argument = launchers.exec_path_argument(alias)
                data = (f"[Desktop Entry]\nType=Application\nName={app}\nIcon={icon}\n"
                        f"Exec=/usr/bin/gio launch {argument}\nTerminal=false\n")
                try:
                    fd = os.open(target, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o700)
                except FileExistsError:
                    pass
                else:
                    with os.fdopen(fd, "w", encoding="utf-8") as out:
                        out.write(data)
                        out.flush()
                        os.fchmod(out.fileno(), 0o700)
                        if os.path.samestat(os.fstat(out.fileno()), target.lstat()):
                            result = subprocess.run([gio, "set", "-n", "-t", "string", str(target), "metadata::trusted", "true"],
                                                    capture_output=True, timeout=8)
                            installed = True
                            if result.returncode:
                                print(f"{app} 图标已建立。请在 GNOME 桌面右键选择“允许启动”。", file=sys.stderr)
            with marker.open("x", encoding="utf-8") as record:
                record.write("installed\n" if installed else "preserved\n")
    finally:
        os.close(lock)


if __name__ == "__main__":
    try:
        ensure_shortcuts()
    except (OSError, RuntimeError, subprocess.TimeoutExpired) as error:
        print(error, file=sys.stderr)
        sys.exit(1)

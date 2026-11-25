#!/usr/bin/python3
"""Offer each installed application a desktop shortcut once, in the GNOME user's session."""
import ctypes
import fcntl
import hashlib
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
    return launchers.native_entry(app) or launchers.application_entry(launchers.APP_IDS[app])


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


ICON_LIMIT = 4 * 1024 * 1024
ICON_OWNER = "user.cloudworkspace.shortcut-icon"


def icon_sources(icon):
    if os.path.isabs(icon):
        yield Path(icon.replace("\\\\", "\\"))
    # The private compositor has not started, so query the configured theme headlessly.
    theme = None
    try:
        import gi
        gi.require_version("Gtk", "3.0")
        from gi.repository import Gio, Gtk
        theme = Gtk.IconTheme.new()
        schemas = Gio.SettingsSchemaSource.get_default()
        if schemas and (schema := schemas.lookup("org.gnome.desktop.interface", True)):
            theme.set_custom_theme(Gio.Settings.new_full(schema, None, None).get_string("icon-theme"))
    except (ImportError, ValueError, RuntimeError):
        pass
    for name in dict.fromkeys((icon, launchers.GENERIC_ICON)):
        if theme and "/" not in name:
            match = theme.lookup_icon(name, 512, 0)
            if match and match.get_filename():
                yield Path(match.get_filename())
        if name in ("claude-desktop", "chatgpt"):
            for base in launchers.data_roots():
                yield base / "icons/hicolor/256x256/apps" / (name + ".png")
                yield base / "pixmaps" / (name + ".png")
    # Noble ships these fallback images even when the calling Python lacks GI.
    for theme_dir in ("Yaru/256x256@2x", "Yaru/256x256", "Adwaita/16x16"):
        yield Path("/usr/share/icons") / theme_dir / "mimetypes/application-x-executable.png"


def png_artwork(icon):
    for source in icon_sources(icon):
        try:
            if not source.is_file() or source.stat().st_size > ICON_LIMIT:
                continue
            if source.suffix.lower() == ".png":
                if launchers.valid_png(source.resolve()):
                    with source.open("rb") as stream:
                        data = stream.read(ICON_LIMIT + 1)
                    if len(data) <= ICON_LIMIT:
                        return data  # Preserve the original raster size, alpha and bytes.
            elif source.suffix.lower() in (".svg", ".xpm"):
                from gi.repository import GdkPixbuf, GLib
                try:
                    image = GdkPixbuf.Pixbuf.new_from_file_at_scale(str(source), 512, 512, True)
                    ok, data = image.save_to_bufferv("png", [], [])
                    if ok:
                        return data
                except GLib.Error:
                    pass
        except (OSError, ImportError, ValueError):
            continue
    return None


def icon_identity(info, data):
    # An ordinary copy, including one made with cp -a, is a different file.
    return f"{info.st_ino}:{hashlib.sha256(data).hexdigest()}".encode("ascii")


def shortcut_icon(alias, icon, *, create=False):
    path = alias.with_suffix(".png")
    try:
        try:
            opened = open(path, "rb", opener=lambda name, flags: os.open(name, flags | os.O_NOFOLLOW | os.O_NONBLOCK))
        except FileNotFoundError:
            if not create:
                return icon  # Older releases made one-time desktop entries with a literal Icon.
            previous, old = None, None
        else:
            with opened as existing:
                fd = existing.fileno()
                previous = os.fstat(fd)
                if not stat.S_ISREG(previous.st_mode) or previous.st_size > ICON_LIMIT:
                    return icon
                old = existing.read(ICON_LIMIT + 1)
                if os.getxattr(fd, ICON_OWNER) != icon_identity(previous, old):
                    return icon
        data = png_artwork(icon)
        if data and data != old:
            with tempfile.TemporaryDirectory(dir=path.parent) as work:
                staged = Path(work) / "icon"
                staged.write_bytes(data)
                staged.chmod(0o600)
                os.setxattr(staged, ICON_OWNER, icon_identity(staged.stat(), data))
                if previous is None:
                    os.link(staged, path)
                elif os.path.samestat(previous, path.lstat()):
                    staged.replace(path)
                else:
                    return icon
            old = data
        if old:
            return str(path).replace("\\", "\\\\")
    except OSError:
        pass  # Missing artwork, unsupported xattrs or a foreign file must not prevent launching.
    return icon


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
    try:
        launchers.offer_app_setup()
    except (OSError, RuntimeError) as error:
        print(f"安装官方客户端的菜单项未能更新：{error}", file=sys.stderr)
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
                    alias = stable_entry(app, source, marker)
                    shortcut_icon(alias, icon)
                continue
            if desktop is None:
                continue
            target = desktop / (app + ".desktop")
            # Preserve a vendor shortcut or any file, directory or symlink the user already placed here.
            installed = False
            if not any(os.path.lexists(path) for path in (desktop / source.name, target)):
                alias = stable_entry(app, source, marker)
                icon = shortcut_icon(alias, icon, create=True)
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

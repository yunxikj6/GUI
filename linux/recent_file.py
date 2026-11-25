"""Remember an explicitly opened local working copy in the user's GTK file chooser."""
import argparse
import fcntl
import os
from pathlib import Path


def remember(path):
    import gi
    gi.require_version("Gtk", "3.0")
    from gi.repository import Gio, GLib, Gtk

    if not Gtk.init_check()[0]:
        return
    settings = Gtk.Settings.get_default()
    if not settings.get_property("gtk-recent-files-enabled") or settings.get_property("gtk-recent-files-max-age") == 0:
        return
    schemas = Gio.SettingsSchemaSource.get_default()
    privacy = schemas.lookup("org.gnome.desktop.privacy", True) if schemas else None
    if privacy:
        preferences = Gio.Settings.new_full(privacy, None, None)
        if not preferences.get_boolean("remember-recent-files") or preferences.get_int("recent-files-max-age") == 0:
            return

    base = Path(GLib.get_user_data_dir()) / "cloudworkspace/file-copies"
    if not path.is_relative_to(base) or ".." in path.parts or not path.is_file():
        return
    data = Gtk.RecentData()
    mime, uncertain = Gio.content_type_guess(path.name, None)
    data.mime_type = "text/plain" if uncertain else mime
    data.app_name = "Cloud Linux Workspace"
    data.app_exec = "gio open %u"
    # GTK loads a snapshot of shared history. Serialize our own simultaneous
    # selections on the existing private directory before creating that snapshot.
    fd = os.open(base, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        manager = Gtk.RecentManager.new()
        manager.add_full(path.as_uri(), data)
        manager.run_dispose()  # Flush this short-lived manager before releasing the lock.
    finally:
        os.close(fd)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("path", type=Path)
    try:
        remember(parser.parse_args().path)
    except (ImportError, OSError, ValueError):
        pass  # GTK history is optional.

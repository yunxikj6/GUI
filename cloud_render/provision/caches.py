"""Generate only Ubuntu guest caches after dpkg payloads have been verified and extracted."""

import os
import subprocess

GUEST_SCRIPT = r"""
set -eu
umask 022
PATH=/usr/sbin:/usr/bin:/sbin:/bin
export PATH LC_ALL=C
ldconfig
glib-compile-schemas --strict /usr/share/glib-2.0/schemas
gio-querymodules /usr/lib/x86_64-linux-gnu/gio/modules
if [ -e /usr/lib/x86_64-linux-gnu/gdk-pixbuf-2.0/gdk-pixbuf-query-loaders ]; then
 /usr/lib/x86_64-linux-gnu/gdk-pixbuf-2.0/gdk-pixbuf-query-loaders --update-cache
fi
if [ -e /usr/lib/x86_64-linux-gnu/libgtk-3-0t64/gtk-query-immodules-3.0 ]; then
 /usr/lib/x86_64-linux-gnu/libgtk-3-0t64/gtk-query-immodules-3.0 --update-cache
fi
for theme in Adwaita Yaru hicolor; do
 if [ -f "/usr/share/icons/$theme/index.theme" ]; then gtk-update-icon-cache --force "/usr/share/icons/$theme"; fi
done
fc-cache -fs
if command -v update-desktop-database >/dev/null; then update-desktop-database /usr/share/applications; fi
if command -v update-mime-database >/dev/null; then update-mime-database /usr/share/mime; fi
"""


def generate(root, bwrap):
    root = root.resolve()
    if (
        not (root / "etc/os-release").is_file()
        or not (root / "usr/bin/gnome-shell").is_file()
        or not root.is_dir()
        or root.stat().st_uid != os.geteuid()
    ):
        raise ValueError("expected the selected private Ubuntu guest")
    subprocess.run(
        [
            bwrap,
            "--die-with-parent",
            "--unshare-all",
            "--bind",
            str(root),
            "/",
            "--dev",
            "/dev",
            "--proc",
            "/proc",
            "--tmpfs",
            "/tmp",
            "--tmpfs",
            "/run",
            "--clearenv",
            "--setenv",
            "HOME",
            "/tmp",
            "--chdir",
            "/",
            "--",
            "/bin/sh",
            "-ec",
            GUEST_SCRIPT,
        ],
        check=True,
        timeout=100,
    )

"""Exec GNOME 46 with the display name used by its own nested Clutter stage."""
import os
from pathlib import Path
import re
import sys


def window_title(identifier):
    title = "Cloud Linux Workspace"
    if identifier and re.fullmatch(r"[0-9a-f]{32}", identifier):
        return f"{title} [{identifier}]"
    return title


if __name__ == "__main__":
    directory, shell, *arguments = sys.argv[1:]
    # GLib derives the stage title from basename(argv[0]). Keep argv[0] executable
    # for Shell's own re-exec, and exec the original binary to preserve its kernel name.
    alias = Path(directory) / window_title(os.environ.get("CLOUDWORKSPACE_VERIFY_ID"))
    try:
        alias.symlink_to(os.path.abspath(shell))
    except OSError:
        alias = shell
    os.execv(shell, [str(alias), *arguments])

"""Fresh, private APT build and original GNOME process for the session guardian."""

import os
import secrets
import tempfile
from pathlib import Path

from . import chatgpt
from .apt import resolve
from .caches import generate
from .packages import build


def run(directory, port, token, app_deb=None):
    modules = Path(__file__).resolve().parents[1]
    root = directory / "ubuntu"
    ring = Path(__file__).with_name("keys") / "ubuntu-archive-keyring.gpg"
    # Quarantine and hash the supplied file before any APT network activity.
    archive = chatgpt.stage(app_deb, directory) if app_deb else None
    with tempfile.TemporaryDirectory(dir=directory, prefix="apt-") as scratch:
        packages = resolve(Path(scratch), ring, chatgpt.PACKAGES if app_deb else ())
    print(f"Ubuntu 已验证 {len(packages)} 个软件包的索引，正在准备专用桌面。", flush=True)
    build(packages, root, reserve_bytes=chatgpt.INSTALLED_BYTES if archive else 0)
    # GNOME Shell depends on bubblewrap in Noble. Use that signed executable on
    # the Debian host too. Its system ABI is available on our supported host.
    bwrap = root / "usr/bin/bwrap"
    if bwrap.is_symlink() or not bwrap.is_file() or bwrap.stat().st_mode & 0o6000:
        raise ValueError("Ubuntu 的非特权 bubblewrap 校验失败。")
    if archive:
        chatgpt.install(root, archive)
    generate(root, bwrap)
    machine_id = directory / "machine-id"
    machine_id.write_text(secrets.token_hex(16) + "\n")
    args = [
        str(bwrap),
        "--die-with-parent",
        "--unshare-user",
        "--unshare-pid",
        "--unshare-ipc",
        "--unshare-uts",
        "--ro-bind",
        str(root),
        "/",
        "--dev",
        "/dev",
        "--proc",
        "/proc",
        "--ro-bind",
        "/sys",
        "/sys",
        "--tmpfs",
        "/tmp",
        "--tmpfs",
        "/run",
        "--ro-bind",
        str(modules),
        "/run/app/cloud_render",
        "--ro-bind",
        str(machine_id),
        "/etc/machine-id",
    ]
    if app_deb:
        args += ["--ro-bind", str(modules.parent / "experiments"), "/run/app/experiments"]
        service = ["experiments.original_desktop_source", "--port", str(port)]
    else:
        service = ["cloud_render.gnome_relay", "--port", str(port), "--session-mode", "ubuntu"]
    args += ["--chdir", "/run/app", "--", "/usr/bin/python3", "-B", "-m", *service]
    os.execve(bwrap, args, {"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8", "CLOUDWORKSPACE_GNOME_TOKEN": token})

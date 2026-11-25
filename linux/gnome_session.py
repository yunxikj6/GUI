"""Start a nested, local GNOME Shell on the user's existing graphical display."""
import argparse
import ctypes
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys
import tempfile


class LaunchError(Exception):
    pass


def executable(name):
    path = shutil.which(name)
    if not path:
        raise LaunchError(f"找不到 {name}。请在当前 Ubuntu 发行版中安装对应组件。")
    return path


def probe(args, label):
    try:
        result = subprocess.run(args, stdin=subprocess.DEVNULL, capture_output=True, timeout=8,
                                text=True, errors="replace", env={**os.environ, "LC_ALL": "C"})
    except subprocess.TimeoutExpired as exc:
        raise LaunchError(f"{label}超时。") from exc
    if result.returncode:
        raise LaunchError(label + "失败。" + result.stderr.strip()[:350])
    return result.stdout


def has_x_display():
    if not os.environ.get("DISPLAY"):
        return False
    # A listening socket alone does not establish that this user can authenticate.
    command = shutil.which("xdpyinfo")
    if command:
        try:
            probe([command], "连接父 X11 显示")
            return True
        except LaunchError:
            return False
    # GLX-capable Mutter already links Xlib, even on minimal systems without xdpyinfo.
    try:
        xlib = ctypes.CDLL("libX11.so.6")
        xlib.XOpenDisplay.argtypes = [ctypes.c_char_p]
        xlib.XOpenDisplay.restype = ctypes.c_void_p
        display = xlib.XOpenDisplay(None)
        if display:
            xlib.XCloseDisplay.argtypes = [ctypes.c_void_p]
            xlib.XCloseDisplay(display)
            return True
    except OSError:
        pass
    return False


def socket_path(value, directory):
    if not value:
        return None
    path = Path(value)
    if not path.is_absolute():
        if not directory:
            return None
        path = Path(directory) / path
    return path if path.is_socket() else None


def select_backend(flags, x_display, wayland_display):
    if "--nested" in flags and x_display:
        return "--nested"
    if "--devkit" in flags and (wayland_display or x_display):
        return "--devkit"
    if "--nested" in flags:
        raise LaunchError("此版 GNOME 的嵌套窗口需要可连接的 X11 DISPLAY。请检查 WSLg 的 XWayland 和显示授权。")
    if "--devkit" in flags:
        raise LaunchError("未找到可用的父 Wayland 或 X11 显示。请检查 WSLg 与本地显示环境。")
    raise LaunchError("此版 Mutter 没有提供 --nested 或 --devkit 嵌套后端。")


def session_mode():
    directories = os.environ.get("XDG_DATA_DIRS") or "/usr/local/share:/usr/share"
    return "ubuntu" if any((Path(d) / "gnome-shell/modes/ubuntu.json").is_file()
                           for d in directories.split(os.pathsep) if d) else "user"


def main():
    parser = argparse.ArgumentParser(description="在当前 WSLg 或 Linux 图形显示中打开本机 GNOME Shell。")
    parser.add_argument("--check", action="store_true", help="检查依赖和父显示后退出")
    parser.add_argument("--desktop-shortcuts", action="store_true", help="为已经注册的应用首次建立 GNOME 桌面快捷方式")
    args = parser.parse_args()
    shell = executable("gnome-shell")
    bus = executable("dbus-run-session")
    gdbus = executable("gdbus")
    flags = set(re.findall(r"(?<![\w-])--[\w-]+", probe([shell, "--help-all"], "读取 GNOME Shell 启动参数")))
    if "--wayland-display" not in flags:
        raise LaunchError("GNOME Shell 没有提供独立 Wayland socket 的命名参数。")
    runtime = os.environ.get("XDG_RUNTIME_DIR")
    if runtime and (not Path(runtime).is_absolute() or not os.access(runtime, os.W_OK | os.X_OK)):
        raise LaunchError("当前用户无法使用 XDG_RUNTIME_DIR。请从当前 Ubuntu 用户的图形会话启动。")
    wayland = socket_path(os.environ.get("WAYLAND_DISPLAY"), runtime)
    backend = select_backend(flags, has_x_display(), wayland)
    if backend == "--devkit":
        # Mutter itself starts this helper from its build-time libexec directory.
        helpers = [Path(shell).resolve().parents[1] / "libexec/mutter-devkit", Path("/usr/libexec/mutter-devkit"),
                   *Path("/usr/lib").glob("*/mutter-*/mutter-devkit"), *Path("/usr/lib").glob("mutter-*/mutter-devkit")]
        if not any(os.access(path, os.X_OK) for path in helpers):
            raise LaunchError("该 Mutter 需要发行版的 mutter-devkit 查看器。请安装匹配版本。")
        pipewire_runtime = os.environ.get("PIPEWIRE_RUNTIME_DIR") or runtime
        if not socket_path(os.environ.get("PIPEWIRE_REMOTE") or "pipewire-0", pipewire_runtime):
            raise LaunchError("Mutter devkit 需要本机 PipeWire。请先启动当前用户的 PipeWire 服务。")
    probe([gdbus, "call", "--system", "--timeout", "3", "--dest", "org.freedesktop.DBus",
           "--object-path", "/org/freedesktop/DBus", "--method", "org.freedesktop.DBus.GetId"],
          "连接 Ubuntu 的系统 D-Bus（请确认 systemd / D-Bus 已启动）")
    mode = session_mode()
    version = probe([shell, "--version"], "读取 GNOME Shell 版本").strip()
    print(f"{version}  ·  {backend}  ·  {'Ubuntu 会话模式' if mode == 'ubuntu' else 'GNOME 标准会话模式'}", flush=True)
    if args.check:
        return 0
    # Use a short, private path even when the user's real runtime path is long.
    # The ordinary runtime and home remain available for WSLg, media and installed applications.
    with tempfile.TemporaryDirectory(prefix="local-gnome-", dir="/tmp") as private_dir:
        child_display = str(Path(private_dir) / "wayland-0")
        env = dict(os.environ)
        for name in ("DISPLAY", "WAYLAND_SOCKET", "SESSION_MANAGER", "DESKTOP_STARTUP_ID"):
            env.pop(name, None)
        env.update(WAYLAND_DISPLAY=child_display, XDG_RUNTIME_DIR=runtime or private_dir,
                   XDG_SESSION_TYPE="wayland", GNOME_SHELL_SESSION_MODE=mode,
                   XDG_CURRENT_DESKTOP="ubuntu:GNOME" if mode == "ubuntu" else "GNOME")
        # D-Bus activated Wayland applications must inherit the child display. Only the compositor
        # and its viewer need the outer display while constructing the nested window.
        parent_env = ["WAYLAND_DISPLAY=" + str(wayland)] if wayland else ["-u", "WAYLAND_DISPLAY"]
        if os.environ.get("DISPLAY"):
            parent_env.append("DISPLAY=" + os.environ["DISPLAY"])
        cmd = ["env", *parent_env, shell, backend, "--wayland", "--wayland-display=" + child_display]
        if backend == "--nested" and "--sm-disable" in flags:
            cmd.append("--sm-disable")
        # WSLg can mount the outer X11 socket directory read-only. Native Wayland applications
        # continue to work when the nested compositor cannot create its own Xwayland sockets.
        x11_dir = Path("/tmp/.X11-unix")
        if "--no-x11" in flags and (not shutil.which("Xwayland") or
                                    (x11_dir.exists() and not os.access(x11_dir, os.W_OK))):
            cmd.append("--no-x11")
        if args.desktop_shortcuts and mode == "ubuntu":
            # DING reads trust metadata from GVfs on this session's private bus, including on WSLg.
            setup = Path(__file__).with_name("desktop-shortcuts.py")
            cmd = ["/bin/sh", "-c", '"$1" "$2" || echo "部分桌面图标未能建立。" >&2\nshift 2\nexec "$@"',
                   "gnome-shortcuts", sys.executable, str(setup), *cmd]
        session = subprocess.Popen([bus, "--", *cmd], env=env, start_new_session=True)

        terminating = False

        def stop(signum, frame):
            nonlocal terminating
            terminating = True
            try:
                os.killpg(session.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass

        previous = {sig: signal.signal(sig, stop) for sig in (signal.SIGHUP, signal.SIGINT, signal.SIGTERM)}
        try:
            result = session.wait()
        finally:
            for sig, handler in previous.items():
                signal.signal(sig, handler)
        if result and not terminating:
            print("GNOME Shell 已退出，请查看上面的 Mutter 或系统服务错误。", file=sys.stderr)
        return result if result >= 0 else 128 - result


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (LaunchError, OSError) as error:
        print(error, file=sys.stderr)
        sys.exit(1)

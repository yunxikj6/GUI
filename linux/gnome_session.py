"""Start a nested, local GNOME Shell on the user's existing graphical display."""
import argparse
import ctypes
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile


class LaunchError(Exception):
    pass


def configure_chatgpt_display(env, private_dir, child_display):
    # OpenAI's desktop entry invokes "chatgpt %U". DING can pass the outer X11
    # display as well as the nested Wayland socket. Select the documented native
    # backend for this session, including Activities, file opens and the Dock.
    system_path = env.get("CLOUDWORKSPACE_SYSTEM_PATH", env.get("PATH", os.defpath))
    command = shutil.which("chatgpt", path=system_path) or "/usr/bin/chatgpt"
    directory = Path(private_dir) / "app-bin"
    directory.mkdir(mode=0o700)
    launcher = directory / "chatgpt"
    launcher.write_text(
        "#!/bin/sh\nunset DISPLAY WAYLAND_SOCKET\n"
        f"export WAYLAND_DISPLAY={shlex.quote(child_display)}\n"
        f'exec {shlex.quote(command)} --ozone-platform=wayland "$@"\n',
        encoding="utf-8",
    )
    launcher.chmod(0o700)
    env["CLOUDWORKSPACE_SYSTEM_PATH"] = system_path
    env["PATH"] = str(directory) + os.pathsep + system_path


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


def parent_workarea():
    """Return the primary X11 monitor's usable rectangle in the parent's coordinate space."""
    class Rectangle(ctypes.Structure):
        _fields_ = [(field, ctypes.c_int) for field in ("x", "y", "width", "height")]

    pointer = ctypes.c_void_p
    try:
        gdk = ctypes.CDLL("libgdk-3.so.0")
        for name, args, result in (
            ("gdk_display_open", [ctypes.c_char_p], pointer),
            ("gdk_x11_display_set_window_scale", [pointer, ctypes.c_int], None),
            ("gdk_display_get_primary_monitor", [pointer], pointer),
            ("gdk_display_get_monitor", [pointer, ctypes.c_int], pointer),
            ("gdk_monitor_get_workarea", [pointer, ctypes.POINTER(Rectangle)], None),
            ("gdk_display_close", [pointer], None),
        ):
            function = getattr(gdk, name)
            function.argtypes, function.restype = args, result
    except (OSError, AttributeError):
        return None
    name = os.environ.get("DISPLAY")
    if not name:
        return None
    # This launcher is single-threaded. Query the parent X11 display even if the
    # user's apps prefer Wayland, then restore their original backend preference.
    previous_backend = os.environ.get("GDK_BACKEND")
    try:
        os.environ["GDK_BACKEND"] = "x11"
        display = gdk.gdk_display_open(os.fsencode(name))
    finally:
        if previous_backend is None:
            del os.environ["GDK_BACKEND"]
        else:
            os.environ["GDK_BACKEND"] = previous_backend
    if not display:
        return None
    try:
        # GDK's application-pixel scale applies only to this query connection. Mutter
        # expects X11 window coordinates, which WSLg may already have scaled.
        gdk.gdk_x11_display_set_window_scale(display, 1)
        monitor = gdk.gdk_display_get_primary_monitor(display) or gdk.gdk_display_get_monitor(display, 0)
        if monitor:
            area = Rectangle()
            gdk.gdk_monitor_get_workarea(monitor, ctypes.byref(area))
            if area.width > 0 and area.height > 0:
                return area.width, area.height
    finally:
        gdk.gdk_display_close(display)
    return None


def nested_x11_46(version, backend):
    return backend == "--nested" and bool(re.match(r"^GNOME Shell 46(?:\.|$)", version))


def nested_mode(version, backend):
    # GNOME 46's X11 nesting backend defaults to a fixed 800x600 fake monitor.
    if not nested_x11_46(version, backend):
        return None
    if any(key.startswith(("MUTTER_DEBUG_DUMMY_", "MUTTER_DEBUG_NUM_DUMMY_", "MUTTER_DEBUG_TILED_DUMMY_"))
           for key in os.environ):
        return None
    area = parent_workarea()
    if not area:
        return None
    width, height = area
    # Leave space for the parent window's borders and title bar, including on small displays.
    return f"{width - min(32, width // 12)}x{height - min(64, height // 8)}"


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


def app_observer_env(env, directory, version, backend, mode):
    """Keep the verification extension and its Ubuntu mode in this compositor's lifetime."""
    token = env.pop("CLOUDWORKSPACE_VERIFY_ID", "")
    enabled = env.pop("CLOUDWORKSPACE_VERIFY_APPS", "")
    if not re.fullmatch(r"[0-9a-f]{32}", token):
        return []
    marker = "CLOUDWORKSPACE_VERIFY_ID=" + token
    if enabled != "1":
        return [marker]  # The existing Nautilus verifier needs only the compositor marker.
    if not nested_x11_46(version, backend) or mode != "ubuntu":
        raise LaunchError("应用窗口验收需要 Ubuntu 的 GNOME Shell 46 嵌套会话。")
    search = env.get("XDG_DATA_DIRS") or "/usr/local/share:/usr/share"
    sources = (Path(p) / "gnome-shell/modes/ubuntu.json" for p in search.split(os.pathsep) if p)
    source = next((path for path in sources if path.is_file()), None)
    if not source:
        raise LaunchError("无法找到 Ubuntu 的 GNOME 会话模式。")
    try:
        base = json.loads(source.read_text(encoding="utf-8"))
        extensions = base.get("enabledExtensions", [])
        if not isinstance(extensions, list) or not all(isinstance(s, str) for s in extensions):
            raise ValueError
    except (ValueError, AttributeError, OSError) as exc:
        raise LaunchError("无法读取 Ubuntu 的 GNOME 会话模式，应用窗口验收未启动。") from exc

    data = Path(directory) / "verification-data"
    uuid = f"app-focus-{token}@cloudworkspace.local"
    mode_dir = data / "gnome-shell/modes"
    ext_dir = data / "gnome-shell/extensions" / uuid
    mode_dir.mkdir(parents=True)
    ext_dir.mkdir(parents=True)
    base["enabledExtensions"] = [*extensions, uuid]
    (mode_dir / "ubuntu.json").write_text(json.dumps(base), encoding="utf-8")
    metadata = {"uuid": uuid, "name": "Workspace application verification", "shell-version": ["46"],
                "workspace-display": str(Path(directory) / "wayland-0"),
                "description": "Read the focused vendor window identity during a marked verification session."}
    (ext_dir / "metadata.json").write_text(json.dumps(metadata), encoding="utf-8")
    shutil.copyfile(Path(__file__).with_name("verify_app_focus.js"), ext_dir / "extension.js")
    return [marker, "CLOUDWORKSPACE_VERIFY_APPS=1", f"XDG_DATA_DIRS={data}:{search}"]


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
                   CLOUDWORKSPACE_NESTED_GNOME="1",
                   XDG_SESSION_TYPE="wayland", GNOME_SHELL_SESSION_MODE=mode,
                   XDG_CURRENT_DESKTOP="ubuntu:GNOME" if mode == "ubuntu" else "GNOME")
        configure_chatgpt_display(env, private_dir, child_display)
        # D-Bus activated Wayland applications must inherit the child display. Only the compositor
        # and its viewer need the outer display while constructing the nested window.
        parent_env = ["WAYLAND_DISPLAY=" + str(wayland)] if wayland else ["-u", "WAYLAND_DISPLAY"]
        if os.environ.get("DISPLAY"):
            parent_env.append("DISPLAY=" + os.environ["DISPLAY"])
        parent_env.extend(app_observer_env(env, private_dir, version, backend, mode))
        initial_mode = nested_mode(version, backend)
        if initial_mode:
            parent_env.append("MUTTER_DEBUG_DUMMY_MODE_SPECS=" + initial_mode)
        compositor = [shell]
        if nested_x11_46(version, backend):
            compositor = [sys.executable, str(Path(__file__).with_name("nested_window.py")), private_dir, shell]
        cmd = ["env", *parent_env, *compositor, backend, "--wayland", "--wayland-display=" + child_display]
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

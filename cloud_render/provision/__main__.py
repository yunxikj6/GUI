"""在 Debian 13 云机上启动并回收一次性原生 Ubuntu 桌面。"""

import argparse
import contextlib
import os
import platform
import secrets
import shutil
import socket
import subprocess
import sys
from pathlib import Path

from .guardian import MODULE


def prerequisites(port):
    if os.name != "posix" or platform.machine() != "x86_64" or os.getuid() == 0:
        raise ValueError("请在云机的普通 x86-64 用户会话中运行。")
    os_info = platform.freedesktop_os_release()
    if os_info.get("ID") != "debian" or os_info.get("VERSION_ID") != "13" or sys.version_info < (3, 12):
        raise ValueError("此入口需要 Debian 13、Python 3.12 或更高版本。")
    for program in ("apt-get", "apt-cache", "dpkg-deb", "unshare"):
        if shutil.which(program) is None:
            raise ValueError("云机缺少 " + program)
    if shutil.disk_usage("/tmp").free < 1500 * 1024**2:
        raise ValueError("临时目录至少需要 1.5 GiB 空闲空间。")
    if subprocess.run(
        ["unshare", "--user", "--mount", "--", "/usr/bin/true"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    ).returncode:
        raise ValueError("云机禁止此账户创建所需的用户和挂载命名空间。")
    # Refuse an occupied port before spending time and bandwidth on the build.
    if port:
        with socket.socket() as check:
            check.bind(("127.0.0.1", port))


@contextlib.contextmanager
def _terminal():
    with open("/dev/tty", "w", encoding="utf-8") as terminal:
        yield terminal


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--port", type=int, help="云机回环端口，桌面默认 17325，应用桌面默认 17326，0 表示使用空闲端口"
    )
    parser.add_argument(
        "--lifetime-minutes", type=int, default=240, help="从准备开始计算的最长存活分钟数，默认 240"
    )
    application = parser.add_mutually_exclusive_group()
    application.add_argument("--chatgpt", action="store_true", help="在云机获取并运行已固定版本的原厂 ChatGPT 软件包")
    application.add_argument("--chatgpt-deb", type=Path, metavar="FILE",
                        help="在云机的临时 Ubuntu 桌面运行已核对的原版 Linux amd64 安装包")
    parser.add_argument("--profile", type=Path, help="保存原厂应用数据的云端专用目录，默认随会话回收")
    args = parser.parse_args(argv)
    if args.profile is not None and not (args.chatgpt_deb or args.chatgpt):
        parser.error("--profile 需要同时指定 --chatgpt 或 --chatgpt-deb。")
    if args.port is None:
        args.port = 17326 if args.chatgpt_deb or args.chatgpt else 17325
    extra = ["--chatgpt"] if args.chatgpt else []
    if args.chatgpt_deb:
        from .chatgpt import PACKAGE
        source = args.chatgpt_deb.absolute()
        if not source.is_file() or source.stat().st_size != PACKAGE["size"]:
            parser.error("请提供已核对版本的云端 ChatGPT Linux amd64 .deb。")
        extra = [str(source)]
    if args.profile is not None:
        extra += ["--profile", str(args.profile.absolute())]
    if not 0 <= args.port <= 65535 or not 1 <= args.lifetime_minutes <= 1440:
        parser.error("端口须在 0 至 65535 之间，生命周期须在 1 至 1440 分钟之间。")
    handles = contextlib.ExitStack()
    tty = None
    supplied = os.environ.pop("CLOUDWORKSPACE_GNOME_TOKEN", None)
    try:
        if supplied is not None:
            import re

            if not re.fullmatch(r"[A-Za-z0-9_-]{43}", supplied):
                raise ValueError("云端桌面配对码格式无效。")
        else:
            try:
                tty = handles.enter_context(_terminal())
            except OSError:
                raise ValueError("自动化使用时请通过私有环境设置 CLOUDWORKSPACE_GNOME_TOKEN。") from None
        prerequisites(args.port)
        # Point the child at this very checkout, including when invoked via PYTHONPATH.
        repo = Path(__file__).resolve().parents[2]
        child_env = {"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"}
        for key in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "NO_PROXY", "no_proxy"):
            if key in os.environ:
                child_env[key] = os.environ[key]
        child_env["PYTHONPATH"] = str(repo)
        proc = subprocess.Popen(
            [sys.executable, "-B", "-m", MODULE, "watch", str(args.port), str(args.lifetime_minutes * 60), *extra],
            cwd=repo,
            env=child_env,
            stdin=subprocess.PIPE,
            start_new_session=True,
        )
        try:
            token = supplied or secrets.token_urlsafe(32)
            proc.stdin.write((token + "\n").encode("ascii"))
            proc.stdin.flush()
            if tty:
                print("本次云桌面配对码 " + token, file=tty, flush=True)
            return proc.wait()
        except KeyboardInterrupt:
            return 130
        finally:
            with contextlib.suppress(BrokenPipeError):
                proc.stdin.close()
            # The pipe also closes automatically if the caller loses its terminal.
            proc.wait()
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    finally:
        handles.close()


if __name__ == "__main__":
    raise SystemExit(main())

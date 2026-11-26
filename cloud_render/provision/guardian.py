"""Keep the new directory owned by a process independent of the calling terminal."""

import contextlib
import os
import re
import selectors
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

MODULE = "cloud_render.provision.guardian"
DESKTOP_PROFILE_MARKER = ".cloudworkspace-original-desktop-v1"


def _exited(pid):
    # WNOWAIT keeps the group leader's PID reserved until all of its helpers
    # have been signalled, even if the worker crashed before the guardian.
    return os.waitid(os.P_PID, pid, os.WEXITED | os.WNOHANG | os.WNOWAIT)


def supervise(port, lifetime, app=(), profile=None):
    return watch_private_worker(MODULE, port, lifetime, prefix="ubuntu-workspace-", args=app, profile=profile)


def watch_private_worker(module, port, lifetime, *, prefix, timeout_status=0, args=(), profile=None):
    """Own a private /tmp tree and process group until the controller pipe closes."""
    token = sys.stdin.buffer.readline(46)
    if not re.fullmatch(rb"[A-Za-z0-9_-]{43}\n", token):
        raise ValueError("配对输入无效。")
    # The guardian owns both the directory and the unreaped direct worker.
    # Losing the terminal controller closes its pipe even after SIGKILL.
    with contextlib.ExitStack() as scope:
        fds = ()
        if profile is not None:
            from cloud_render.chatgpt_process import _own_profile
            directory = scope.enter_context(_own_profile(profile, marker_name=DESKTOP_PROFILE_MARKER))
            fds = (directory,)
            args = (*args, "--profile-fd", str(directory))
        workspace = scope.enter_context(tempfile.TemporaryDirectory(prefix=prefix, dir="/tmp"))
        proc = subprocess.Popen(
            [sys.executable, "-m", module, "worker", workspace, str(port), *args],
            stdin=subprocess.PIPE, pass_fds=fds,
            start_new_session=True,
        )
        try:
            proc.stdin.write(token)
            proc.stdin.close()
            deadline = time.monotonic() + lifetime
            stopping = False

            def stop(signum, frame):
                nonlocal stopping
                stopping = True

            for signum in (signal.SIGHUP, signal.SIGTERM, signal.SIGINT):
                signal.signal(signum, stop)
            with selectors.DefaultSelector() as selector:
                selector.register(sys.stdin.buffer, selectors.EVENT_READ)
                while not stopping and _exited(proc.pid) is None:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        if timeout_status:
                            print("本次云桌面已达到设置的最长运行时间。", file=sys.stderr)
                        return timeout_status
                    if selector.select(min(1.0, remaining)):
                        break
            status = _exited(proc.pid)
            return status.si_status if status is not None else 0
        finally:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(proc.pid, signal.SIGTERM)
            grace_end = time.monotonic() + 8
            while _exited(proc.pid) is None and time.monotonic() < grace_end:
                time.sleep(0.1)
            # The still-unreaped PID cannot have become somebody else's group.
            with contextlib.suppress(ProcessLookupError):
                os.killpg(proc.pid, signal.SIGKILL)
            proc.wait()
            print("本次云桌面已关闭，正在回收它的临时目录。", flush=True)


def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("role", choices=("watch", "worker"))
    parser.add_argument("scope")
    parser.add_argument("number", type=int)
    parser.add_argument("archive", nargs="?", type=Path)
    parser.add_argument("--chatgpt", action="store_true")
    parser.add_argument("--profile", type=Path)
    parser.add_argument("--profile-fd", type=int)
    args = parser.parse_args()
    if args.chatgpt and args.archive:
        parser.error("只能选择一个原厂软件包来源。")
    app = "--chatgpt" if args.chatgpt else args.archive
    if args.role == "watch" and args.profile_fd is None:
        return supervise(int(args.scope), args.number, (str(app),) if app else (), args.profile)
    if args.role == "worker" and args.profile is None:
        from .worker import run

        token = sys.stdin.buffer.readline(46)
        if not re.fullmatch(rb"[A-Za-z0-9_-]{43}\n", token):
            raise ValueError("配对输入无效。")
        run(Path(args.scope), args.number, token.rstrip().decode("ascii"), app, args.profile_fd)
        return 0
    raise ValueError("请使用 python3 -m cloud_render.provision 启动。")


if __name__ == "__main__":
    raise SystemExit(main())

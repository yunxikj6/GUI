"""在已安装原厂应用与 GNOME 的 Ubuntu 云机上启动一次配对桌面。"""

import argparse
import asyncio
import contextlib
import getpass
import os
from pathlib import Path
import signal
import subprocess
import sys
import warnings

from cloud_render.gnome_relay import TOKEN
from cloud_render.provision.guardian import watch_private_worker

MODULE = "experiments.original_desktop_source"
ROOT = Path(__file__).resolve().parents[1]


def _token():
    value = os.environ.pop("CLOUDWORKSPACE_GNOME_TOKEN", None)
    if value is None:
        try:
            with open("/dev/tty", "w", encoding="utf-8") as terminal, warnings.catch_warnings():
                warnings.simplefilter("error", getpass.GetPassWarning)
                value = getpass.getpass("本次桌面的 43 位随机配对码（隐藏输入）：", stream=terminal)
        except (OSError, EOFError, getpass.GetPassWarning):
            raise ValueError("请在私有终端输入配对码，或通过私有环境设置 CLOUDWORKSPACE_GNOME_TOKEN。") from None
    if not TOKEN.fullmatch(value):
        raise ValueError("云端桌面配对码需要 43 位 URL 安全随机字符。")
    return value


async def _session(workspace, port, token):
    from experiments.original_desktop_bridge import serve

    channel = workspace / "pair"
    source = workspace / "source"
    # The source, its PID-namespace owner and the bridge share the worker's
    # guardian-owned group. Flooded logs or a missing client cannot extend it.
    proc = await asyncio.create_subprocess_exec(
        "bash", ROOT / "experiments/mutter_app/run.sh", "--interactive", channel, source,
        stdin=subprocess.DEVNULL,
    )
    bridge = asyncio.create_task(serve(channel, port, token))
    exited = asyncio.create_task(proc.wait())
    try:
        done, _ = await asyncio.wait((bridge, exited), return_when=asyncio.FIRST_COMPLETED)
        if bridge in done:
            await bridge
            # Closing a paired connection also closes both private FIFOs. Allow
            # the original source to acknowledge that EOF and release its session.
            try:
                await asyncio.wait_for(asyncio.shield(exited), 3)
            except asyncio.TimeoutError:
                return 0
        if proc.returncode or not (source / "controller-closed").is_file():
            raise RuntimeError("原厂桌面源意外退出，请检查云机的 Ubuntu、ChatGPT、GNOME 及运行依赖。")
        return 0
    finally:
        bridge.cancel()
        await asyncio.gather(bridge, return_exceptions=True)
        if proc.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                proc.terminate()
        exited.cancel()
        await asyncio.gather(exited, return_exceptions=True)


def _worker(workspace, port):
    token = sys.stdin.buffer.readline(46)
    if len(token) != 44 or token[-1:] != b"\n":
        raise ValueError("配对输入无效。")
    value = token[:-1].decode("ascii")
    if not TOKEN.fullmatch(value):
        raise ValueError("配对输入无效。")
    return asyncio.run(_session(workspace, port, value))


def _launch(port, lifetime, token):
    # Include a source Python venv, when present. Vendor and account credentials
    # are never inherited by bash, the renderer or an app launched from the Dock.
    env = {"PATH": os.environ.get("PATH", os.defpath), "LANG": "C.UTF-8", "PYTHONDONTWRITEBYTECODE": "1"}
    if "CW_MUTTER_PREBUILD" in os.environ:
        env["CW_MUTTER_PREBUILD"] = os.environ["CW_MUTTER_PREBUILD"]
    proc = subprocess.Popen(
        [sys.executable, "-m", MODULE, "watch", str(port), str(lifetime)],
        cwd=ROOT, env=env, stdin=subprocess.PIPE, start_new_session=True,
    )
    try:
        proc.stdin.write((token + "\n").encode("ascii"))
        proc.stdin.flush()
        return proc.wait()
    finally:
        with contextlib.suppress(BrokenPipeError):
            proc.stdin.close()
        proc.wait()


def _stop(signum, frame):
    raise SystemExit(128 + signum)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=17326, help="云机回环端口，0 表示自动选择")
    parser.add_argument("--lifetime", type=int, default=3600, help="从启动开始计算的最长运行秒数，默认 3600")
    args = parser.parse_args(argv)
    if not 0 <= args.port <= 65535 or args.lifetime <= 0:
        parser.error("端口须在 0 至 65535 之间，最长运行秒数须为正整数。")
    return _launch(args.port, args.lifetime, _token())


if __name__ == "__main__":
    try:
        if len(sys.argv) == 4 and sys.argv[1] == "worker":
            status = _worker(Path(sys.argv[2]), int(sys.argv[3]))
        elif len(sys.argv) == 4 and sys.argv[1] == "watch":
            status = watch_private_worker(
                MODULE, int(sys.argv[2]), int(sys.argv[3]), prefix="original-ubuntu-", timeout_status=124,
            )
        else:
            for sig in (signal.SIGTERM, signal.SIGHUP):
                signal.signal(sig, _stop)
            status = main()
    except KeyboardInterrupt:
        status = 130
    except (OSError, RuntimeError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        status = 1
    raise SystemExit(status)

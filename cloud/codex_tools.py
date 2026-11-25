"""Codex CLI process and tool settings shared by read-only tasks."""
from contextlib import contextmanager
from array import array
import fcntl
import json
import os
import re
import select
import signal
import subprocess
import termios


_INVENTORY_TIMEOUT = 15
_INVENTORY_LIMIT = 256 * 1024
_DISABLE_EXTENSIONS = ["--disable", "apps", "--disable", "plugins", "--disable", "hooks"]


def output_chunks(proc):
    """Read a Linux subprocess pipe through its producer's exit, including its final writes."""
    fd = proc.stdout.fileno()
    poll = select.poll()
    poll.register(fd, select.POLLIN)
    while proc.poll() is None:
        if poll.poll(100):
            chunk = os.read(fd, 64 * 1024)
            if not chunk:
                return
            yield chunk
    # A background helper may still hold or write the pipe. The direct process has
    # exited, so all of its remaining bytes are already in this kernel queue.
    queued = array("i", [0])
    fcntl.ioctl(fd, termios.FIONREAD, queued)
    remaining = queued[0]
    while remaining:
        chunk = os.read(fd, min(64 * 1024, remaining))
        if not chunk:
            break
        remaining -= len(chunk)
        yield chunk


def output_lines(proc, limit=None):
    line = bytearray()
    for chunk in output_chunks(proc):
        for part in chunk.splitlines(keepends=True):
            line.extend(part)
            if part.endswith((b"\r", b"\n")) or (limit and len(line) >= limit):
                yield line.decode("utf-8", "replace")
                line.clear()
    if line:
        yield line.decode("utf-8", "replace")


@contextmanager
def bounded_process(command, directory, timeout, stdin, stderr, env=None):
    # Runs in the detached Linux worker's main thread. Bound both stdout reads and wait().
    def expired(*_):
        raise TimeoutError
    with subprocess.Popen(command, cwd=directory, stdin=stdin, stdout=subprocess.PIPE,
                          stderr=stderr, env=env, start_new_session=True) as proc:
        previous = signal.signal(signal.SIGALRM, expired)
        signal.setitimer(signal.ITIMER_REAL, timeout)
        try:
            yield proc
        finally:
            signal.setitimer(signal.ITIMER_REAL, 0)
            signal.signal(signal.SIGALRM, previous)
            if proc.poll() is None:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass


def _enabled_servers(directory, env, overrides):
    command = ["codex", "mcp", "list", "--json", *_DISABLE_EXTENSIONS, *overrides]
    with bounded_process(command, directory, _INVENTORY_TIMEOUT, subprocess.DEVNULL, subprocess.DEVNULL, env) as proc:
        # Server descriptors can contain private arguments or headers. Never log or store them.
        raw = bytearray()
        for chunk in output_chunks(proc):
            raw.extend(chunk)
            if len(raw) > _INVENTORY_LIMIT:
                raise ValueError()
        if proc.wait():
            raise ValueError()
        servers = json.loads(raw.decode("utf-8"))
    if not isinstance(servers, list) or len(servers) > 256:
        raise ValueError()
    enabled = set()
    for server in servers:
        if not isinstance(server, dict):
            raise ValueError()
        name = server.get("name")
        if (not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9_:@/.-]{1,256}", name)
                or type(server.get("enabled")) is not bool):
            raise ValueError()
        if server["enabled"]:
            enabled.add(name)
    return enabled


def restricted_tool_args(directory, env=None):
    """Disable servers in the effective config for this cwd, preserving model and auth settings."""
    names = _enabled_servers(directory, env, [])
    overrides = []
    if names:
        entries = ",".join(f'{json.dumps(name)}={{enabled=false}}' for name in sorted(names))
        overrides = ["-c", "mcp_servers={" + entries + "}"]
        if _enabled_servers(directory, env, overrides):
            raise ValueError()
    return [*_DISABLE_EXTENSIONS, *overrides]

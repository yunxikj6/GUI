"""Run a private cloud GNOME session and emit the shell's original Mesa commands."""
import argparse
from collections import deque
from contextlib import ExitStack, closing, contextmanager, suppress
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import select
import shutil
import signal
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time

from .gnome_commands import StreamError, _connect, copy_commands
from .gnome_input import HEIGHT, WIDTH, decode_input, input_size


class SourceError(RuntimeError):
    pass


class Disconnected(Exception):
    pass


_FRAME = struct.Struct('!I32s')
_START_TIMEOUT = 35
_TOOLS = '/usr/bin:/bin'
_STARTED = b'GNOME Shell started at '
_UBUNTU_FILES = (
    'gnome-session/sessions/ubuntu.session',
    'gnome-shell/modes/ubuntu.json',
    'gnome-shell/theme/Yaru/gnome-shell-theme.gresource',
    'gnome-shell/extensions/ubuntu-dock@ubuntu.com/extension.js',
    'backgrounds/warty-final-ubuntu.png',
)

_APP_IDS = {'chatgpt.desktop': 'chatgpt', 'com.anthropic.Claude.desktop': 'claude'}


def _route_launchers(runtime, roots):
    """Keep the distribution's launcher icons and titles, routing activation before any external window starts."""
    destination = runtime / 'home' / '.local' / 'share' / 'applications'
    destination.mkdir(parents=True)
    launcher = runtime / 'activate.py'
    shutil.copyfile(Path(__file__).with_name('gnome_app_request.py'), launcher)
    # Called only for the source's mkdtemp('/tmp/gnome-*') path. Only put
    # validated literal arguments in Desktop Entry Exec fields.
    if not re.fullmatch(r'/tmp/gnome-[a-zA-Z0-9_]+/activate[.]py', str(launcher)):
        raise SourceError('应用入口路径无效。')
    seen = set()
    for root in roots:
        for path in sorted(root.rglob('*.desktop')):
            name = '-'.join(path.relative_to(root).parts)
            if name in seen:
                continue
            seen.add(name)
            if path.stat().st_size > 256 * 1024:
                raise SourceError('发行版应用入口超过大小上限。')
            lines = path.read_text(encoding='utf-8').splitlines()
            if '[Desktop Entry]' not in lines:
                continue
            primary = lines[lines.index('[Desktop Entry]') + 1:]
            primary = primary[:next((i for i, line in enumerate(primary) if line.startswith('[')), len(primary))]
            if 'Type=Application' not in primary:
                continue
            output = []
            group = None
            application = _APP_IDS.get(name, 'wayland')
            exec_line = 'Exec=/usr/bin/python3 ' + str(launcher) + ' ' + application
            for line in lines:
                if line.startswith('['):
                    group = line
                    output.append(line)
                    if group == '[Desktop Entry]':
                        output.extend([exec_line, 'DBusActivatable=false', 'Terminal=false'])
                    continue
                key = line.partition('=')[0].strip()
                if group == '[Desktop Entry]' and key in ('Exec', 'DBusActivatable', 'Terminal'):
                    continue
                if group and group.startswith('[Desktop Action ') and key == 'Exec':
                    line = exec_line
                output.append(line)
            (destination / name).write_text('\n'.join(output) + '\n', encoding='utf-8')


class _CommandOutput:
    """Let the dedicated cloud GPU render ahead of a slow viewer within a fixed budget."""
    def __init__(self, destination, capacity=96 << 20):
        self.destination = destination
        self.capacity = capacity
        self.changed = threading.Condition()
        self.pending = deque()
        self.used = 0
        self.finished = False
        self.failure = None

    def write(self, data):
        if not data:
            return
        if len(data) > self.capacity:
            raise SourceError('单个桌面绘图资源超过队列容量。')
        with self.changed:
            self.changed.wait_for(lambda: self.failure is not None or (
                self.used + len(data) <= self.capacity and len(self.pending) < 8192))
            if self.failure is not None:
                raise self.failure
            self.pending.append(data)
            self.used += len(data)
            self.changed.notify()

    def flush(self):
        # Flush means the preceding submit/readback boundary must be published.
        # The GPU thread can continue while the output thread flushes that boundary.
        with self.changed:
            if not self.pending or self.pending[-1] is not None:
                self.pending.append(None)
                self.changed.notify()

    def finish(self):
        with self.changed:
            self.finished = True
            self.changed.notify_all()

    def publish(self, gate_fd, peer_pid):
        try:
            with os.fdopen(gate_fd, 'rb', closefd=False) as control:
                value = control.read(4)
            if len(value) != 4 or struct.unpack('!I', value) != (peer_pid,):
                raise SourceError('绘图进程与专用 GNOME 会话不符。')
            while True:
                with self.changed:
                    self.changed.wait_for(lambda: self.pending or self.finished)
                    if not self.pending:
                        break
                    data = self.pending.popleft()
                if data is None:
                    self.destination.flush()
                else:
                    self.destination.write(data)
                with self.changed:
                    if data is not None:
                        self.used -= len(data)
                    self.changed.notify_all()
            self.destination.flush()
        except (SourceError, OSError) as exc:
            with self.changed:
                self.failure = exc
                self.pending.clear()
                self.changed.notify_all()


class _ClientGate:
    """Keep ordinary client surfaces off the shell's render command stream."""
    def __init__(self, directory, stack):
        self.named = directory / 'gnome-vgpu'
        self.denied = directory / 'app-requests'
        self.server = stack.enter_context(closing(socket.socket(socket.AF_UNIX)))
        self.server.bind(str(self.denied))
        self.server.listen(8)
        self.server.setblocking(False)
        self.activation = stack.enter_context(closing(socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)))
        self.activation.bind(str(directory / 'activation'))
        self.activation.setblocking(False)

    def seal(self):
        if not self.named.is_socket():
            return False
        # Atomic replacement leaves the compositor's original listening socket
        # without an address. Subsequent normal Wayland connections reach the
        # rejection listener. No Wayland messages or client buffers are proxied.
        os.replace(self.denied, self.named)
        for line in Path('/proc/net/unix').read_text().splitlines()[1:]:
            fields = line.split(maxsplit=7)
            if len(fields) == 8 and fields[7] == str(self.named) and fields[5] != '01':
                raise SourceError('桌面就绪前已有客户连接；没有发布该会话的绘图。')
        return True

    def refused(self):
        try:
            client, _ = self.server.accept()
        except BlockingIOError:
            return None
        with client:
            pid, uid, _ = struct.unpack('3i', client.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
            if uid == os.geteuid():
                for name, path in [('chatgpt', '/usr/lib/chatgpt/ChatGPT'),
                                   ('claude', '/usr/lib/claude-desktop/claude-desktop')]:
                    with suppress(OSError):
                        if os.path.samefile(f'/proc/{pid}/exe', path):
                            return name
            return 'wayland'


def _record(directory, notify, gate_fd):
    """Own the blocking vtest I/O in a child so a stalled sink can be cancelled."""
    frame = 0

    def scanned(desc, pixels):
        nonlocal frame
        _, level, stride, _, x, y, _, width, height, depth, size = desc
        if ((level, x, y, width, height, depth, size) ==
                (0, 0, 0, WIDTH, HEIGHT, 1, WIDTH * HEIGHT * 4) and stride in (0, WIDTH * 4)):
            frame += 1
            os.write(notify, _FRAME.pack(frame, hashlib.sha256(pixels).digest()))

    output = _CommandOutput(sys.stdout.buffer)
    with closing(socket.socket(socket.AF_UNIX)) as server:
        server.bind(str(directory / 'source'))
        server.listen(1)
        with server.accept()[0] as client, _connect(directory / 'gpu') as gpu, client.makefile('rb') as source:
            peer_pid, uid, _ = struct.unpack('3i', client.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
            if uid != os.geteuid() or not os.path.samefile(f'/proc/{peer_pid}/exe', '/usr/bin/gnome-shell'):
                raise SourceError('只有专用 GNOME Shell 可以发布桌面指令。')
            publisher = threading.Thread(target=output.publish, args=(gate_fd, peer_pid), daemon=True)
            publisher.start()
            try:
                copy_commands(source, gpu, recording=output, respond=client.sendall,
                              scanout=scanned, max_bytes=None)
            finally:
                output.finish()
            publisher.join()
            if output.failure is not None:
                raise output.failure


@contextmanager
def _child(command, env, *, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, pass_fds=(), cwd=None):
    p = subprocess.Popen(command, env=env, cwd=cwd, stdin=subprocess.DEVNULL, stdout=stdout,
                         stderr=stderr, pass_fds=pass_fds, start_new_session=True)
    try:
        yield p
    finally:
        # The dedicated session also owns helpers that outlive a group leader.
        with suppress(ProcessLookupError):
            os.killpg(p.pid, signal.SIGTERM)
        try:
            p.wait(timeout=2)
        except subprocess.TimeoutExpired:
            with suppress(ProcessLookupError):
                os.killpg(p.pid, signal.SIGKILL)
            p.wait()
        else:
            with suppress(ProcessLookupError):
                os.killpg(p.pid, signal.SIGKILL)


def _run(env, *args, verify=True):
    return subprocess.run(args, env=env, stdin=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                          stdout=subprocess.PIPE, timeout=4, check=verify, text=True)


def _authority(path):
    # A FamilyWild record in Xauthority permits Xvfb to allocate a free display
    # with -displayfd. Only this session receives the fresh MIT cookie.
    fields = (b'', b'', b'MIT-MAGIC-COOKIE-1', secrets.token_bytes(16))
    with path.open('xb') as f:
        os.chmod(path, 0o600)
        f.write(struct.pack('!H', 65535))
        for data in fields:
            f.write(struct.pack('!H', len(data)))
            f.write(data)


def _window(env):
    result = _run(env, 'xwininfo', '-root', '-tree', verify=False)
    matches = re.findall(r'^\s*(0x[0-9a-f]+) "gnome-shell": [^\n]*?\b800x520[+-]',
                         result.stdout, flags=re.MULTILINE)
    if len(matches) == 1:
        xid = matches[0]
        if 'Map State: IsViewable' in _run(env, 'xwininfo', '-id', xid, verify=False).stdout:
            return xid
    return None


def _focused(env, xid):
    if _window(env) != xid or _run(env, 'xdotool', 'getwindowfocus').stdout.strip() != str(int(xid, 16)):
        raise SourceError('原 GNOME 窗口已失去焦点。')


def _apply(env, xid, command):
    kind, args = decode_input(command)
    if kind in (b'S', b'K'):
        _run(env, 'xdotool', 'key', 'Super_L' if kind == b'S' else args[0])
    else:
        x, y = args
        cmd = ['xdotool', 'mousemove', '--window', xid, str(x), str(y)]
        if kind in (b'L', b'R'):
            cmd.extend(['click', '1' if kind == b'L' else '3'])
        _run(env, *cmd)


class _Source:
    def __init__(self, stack, directory, event_fd, control_fd, mode):
        self.mode = mode
        self.stack = stack
        self.directory = directory
        self.events = stack.enter_context(open(event_fd, 'wb', buffering=0, closefd=False))
        self.control = control_fd
        self.frame_pipe, self.notify = os.pipe()
        stack.callback(os.close, self.frame_pipe)
        stack.callback(os.close, self.notify)
        os.set_blocking(self.frame_pipe, False)
        self.frames = 0
        self.first_frame = None
        self.scene_changed = False
        self.ack = 0
        self.children = []
        self.pending = bytearray()
        self.ready = False
        self.gate = None
        self.shell_log = None
        self.shell_started = False
        self.log_suffix = b''
        self.deadline = time.monotonic() + _START_TIMEOUT
        self.env = dict(PATH=_TOOLS, LANG='C.UTF-8', LC_ALL='C.UTF-8', HOME=str(directory / 'home'),
                        XDG_RUNTIME_DIR=str(directory), XAUTHORITY=str(directory / 'authority'),
                        DBUS_SYSTEM_BUS_ADDRESS='unix:path=' + str(directory / 'system'),
                        GSETTINGS_BACKEND='memory', LIBGL_ALWAYS_SOFTWARE='1', LP_NUM_THREADS='2',
                        GALLIUM_DRIVER='llvmpipe', MUTTER_DEBUG_DUMMY_MODE_SPECS=f'{WIDTH}x{HEIGHT}')

    def emit(self, event, **fields):
        # These short records are atomic on a pipe and include no window contents.
        self.events.write(json.dumps({'event': event, **fields}, separators=(',', ':')).encode() + b'\n')

    def start(self, command, **kwargs):
        env = kwargs.pop('env', self.env)
        p = self.stack.enter_context(_child(command, env, **kwargs))
        self.children.append(p)
        return p

    def checked(self):
        if any(child.poll() is not None for child in self.children):
            raise SourceError('专用 GNOME 会话中的图形进程已退出。')

    def read_frames(self):
        while True:
            try:
                data = os.read(self.frame_pipe, _FRAME.size * 16)
            except BlockingIOError:
                return
            if not data or len(data) % _FRAME.size:
                raise SourceError('云端绘制回执不完整。')
            for n, digest in _FRAME.iter_unpack(data):
                if n != self.frames + 1:
                    raise SourceError('云端绘制回执序号不连续。')
                self.frames = n
                if self.first_frame is None:
                    self.first_frame = digest
                elif self.first_frame != digest:
                    self.scene_changed = True
                self.emit('frame', sequence=n, sha256=digest.hex())

    def poll(self, timeout):
        self.checked()
        inputs = [self.frame_pipe, self.control]
        if self.shell_log is not None:
            inputs.append(self.shell_log)
        if self.gate:
            inputs.extend([self.gate.server, self.gate.activation])
        readable, _, _ = select.select(inputs, [], [], timeout)
        if self.shell_log is not None and self.shell_log in readable:
            chunk = os.read(self.shell_log.fileno(), 4096)
            if not chunk:
                raise SourceError('GNOME Shell 的启动通道已关闭。')
            if not self.shell_started:
                # GNOME 46 logs its public startup-complete milestone in this
                # private stderr pipe after its layout manager commits the scene.
                data = self.log_suffix + chunk
                self.shell_started = _STARTED in data
                self.log_suffix = b'' if self.shell_started else data[-(len(_STARTED) - 1):]
        if self.gate and self.gate.activation in readable:
            app = self.gate.activation.recv(16)
            if app in (b'chatgpt', b'claude', b'wayland'):
                self.emit('unavailable', application=app.decode('ascii'))
        if self.gate and self.gate.server in readable:
            application = self.gate.refused()
            if application:
                self.emit('unavailable', application=application)
        if self.frame_pipe in readable:
            self.read_frames()
        if self.control in readable:
            data = os.read(self.control, 128)
            if not data:
                if self.pending:
                    raise SourceError('控制流在一个输入指令中途关闭。')
                raise Disconnected
            if not self.ready:
                raise SourceError('GNOME 就绪前收到输入。')
            self.pending.extend(data)

    def wait(self, predicate, label):
        while time.monotonic() < self.deadline:
            self.poll(0)
            value = predicate()
            if value:
                return value
            self.poll(.08)
        raise SourceError(f'等待{label}超时。')

    def setup(self):
        runtime = self.directory
        (runtime / 'home' / 'Desktop').mkdir(mode=0o700, parents=True)
        if self.mode == 'ubuntu':
            for item in _UBUNTU_FILES:
                if not (Path('/usr/share') / item).is_file():
                    raise SourceError('Ubuntu 桌面缺少发行版组件 /usr/share/' + item)
        self.env.update(XDG_CURRENT_DESKTOP='ubuntu:GNOME' if self.mode == 'ubuntu' else 'GNOME',
                        XDG_SESSION_TYPE='wayland', DESKTOP_SESSION=self.mode,
                        GNOME_SHELL_SESSION_MODE='ubuntu' if self.mode == 'ubuntu' else 'user')
        _route_launchers(runtime, [Path('/usr/local/share/applications'), Path('/usr/share/applications')])
        _authority(runtime / 'authority')
        config = runtime / 'system.conf'
        config.write_text('<busconfig><type>system</type><listen>unix:path=' + str(runtime / 'system') +
                          '</listen><auth>EXTERNAL</auth><policy context="default"><allow user="*"/>'
                          '<allow send_destination="*"/><allow receive_sender="*"/><allow own="*"/>'
                          '</policy></busconfig>')
        self.start(['dbus-daemon', '--nofork', '--config-file=' + str(config)])
        self.env['DBUS_SESSION_BUS_ADDRESS'] = 'unix:path=' + str(runtime / 'session')
        self.start(['dbus-daemon', '--session', '--nofork', '--address=' + self.env['DBUS_SESSION_BUS_ADDRESS']])
        with ExitStack() as pipes:
            read, write = os.pipe()
            pipes.callback(os.close, read)
            with os.fdopen(write, 'wb'):
                self.start(['Xvfb', '-displayfd', str(write), '-screen', '0', '900x640x24', '-nolisten',
                            'tcp', '-auth', self.env['XAUTHORITY'], '+extension', 'GLX'], pass_fds=(write,))
            self.wait(lambda: select.select([read], [], [], 0)[0], '私有 X 显示器')
            display = os.read(read, 32).strip()
            if not display.isdigit():
                raise SourceError('Xvfb 未返回显示器编号。')
            self.env['DISPLAY'] = ':' + display.decode('ascii')
        self.start(['virgl_test_server', '--no-loop-or-fork', '--use-egl-surfaceless',
                    '--socket-path', str(runtime / 'gpu')])
        self.wait(lambda: (runtime / 'system').is_socket() and (runtime / 'session').is_socket()
                  and (runtime / 'gpu').is_socket(),
                  '专用 GPU 和消息总线')
        self.gate = _ClientGate(runtime, self.stack)
        gate_read, gate_write = os.pipe()
        with os.fdopen(gate_write, 'wb', buffering=0) as release:
            with os.fdopen(gate_read, 'rb'):
                self.start([sys.executable, '-m', 'cloud_render.gnome_source', '--record', str(runtime),
                            '--event-fd', str(self.notify), '--gate-fd', str(gate_read)],
                           pass_fds=(self.notify, gate_read), stdout=sys.stdout.buffer,
                           cwd=Path(__file__).resolve().parents[1])
            self.wait(lambda: (runtime / 'source').is_socket(), '原厂绘图源')
            env = dict(self.env, GALLIUM_DRIVER='virpipe', VTEST_SOCKET_NAME=str(runtime / 'source'))
            shell = self.start(['gnome-shell', '--nested', '--wayland', '--no-x11',
                        '--wayland-display=gnome-vgpu', '--mode=' + env['GNOME_SHELL_SESSION_MODE']],
                               env=env, stderr=subprocess.PIPE)
            self.shell_log = self.stack.enter_context(shell.stderr)
            self.wait(self.gate.seal, '独占的 Wayland 显示入口')
            release.write(struct.pack('!I', shell.pid))
        xid = self.wait(lambda: self.shell_started and self.frames and _window(env),
                        '原 GNOME 窗口')
        if self.mode == 'ubuntu':
            result = _run(env, 'gdbus', 'call', '--session', '--dest', 'org.gnome.Shell',
                          '--object-path', '/org/gnome/Shell', '--method',
                          'org.gnome.Shell.Extensions.GetExtensionInfo', 'ubuntu-dock@ubuntu.com')
            if "'state': <1.0>" not in result.stdout or "'enabled': <true>" not in result.stdout:
                raise SourceError('Ubuntu Dock 没有在发行版会话中启用。')
        self.wait(lambda: self.scene_changed, '启动清屏后的原桌面画面')
        _run(env, 'xdotool', 'windowfocus', '--sync', xid)
        _focused(env, xid)
        self.ready = True
        self.emit('ready', width=WIDTH, height=HEIGHT, frames=self.frames)
        return xid

    def serve(self):
        xid = self.setup()
        while True:
            self.poll(.25)
            while self.pending:
                length = input_size(bytes(self.pending[:1]))
                if len(self.pending) < length:
                    break
                command = bytes(self.pending[:length])
                decode_input(command)
                del self.pending[:length]
                _focused(self.env, xid)
                self.read_frames()
                before = self.frames
                _apply(self.env, xid, command)
                _focused(self.env, xid)
                self.ack += 1
                self.emit('input', sequence=self.ack, framesBefore=before)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--event-fd', type=int, required=True, help='接收就绪、绘制与输入回执的专用写入文件描述符')
    parser.add_argument('--session-mode', choices=('ubuntu', 'gnome'), default='ubuntu',
                        help='默认使用 Ubuntu 发行版会话；gnome 用于诊断基础桌面。')
    parser.add_argument('--record', type=Path, help=argparse.SUPPRESS)
    parser.add_argument('--gate-fd', type=int, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.event_fd < 3 or sys.stdout.isatty() or os.isatty(args.event_fd):
        parser.error('云端源需要独立的指令流和回执管道。')
    try:
        os.fstat(args.event_fd)
        if args.record:
            _record(args.record, args.event_fd, args.gate_fd)
        else:
            def stop(signum, frame):
                raise Disconnected
            signal.signal(signal.SIGTERM, stop)
            with tempfile.TemporaryDirectory(prefix='gnome-', dir='/tmp') as root, ExitStack() as stack:
                _Source(stack, Path(root), args.event_fd, sys.stdin.fileno(), args.session_mode).serve()
    except (Disconnected, KeyboardInterrupt, BrokenPipeError):
        return 0
    except (OSError, SourceError, StreamError, ValueError, subprocess.SubprocessError) as exc:
        message = str(exc) if isinstance(exc, (SourceError, StreamError, ValueError)) else type(exc).__name__
        print('GNOME 云端会话结束。' + message, file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

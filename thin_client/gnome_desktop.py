"""Present a private cloud GNOME session in a local Win32 window."""
import argparse
from collections import deque
from pathlib import Path
import re
import sys
import threading
import time

from cloud_render.gnome_input import DEFAULT_SIZE, SIZE_NAMES, checked_size
from instabox import BridgeError

TITLE = 'Cloud Linux Workspace'
WIDTH, HEIGHT = DEFAULT_SIZE
APPLICATION_STATUS = {
    'chatgpt': '此桌面的 ChatGPT 绘图通道尚未就绪',
    'claude': '此桌面的 Claude 绘图通道尚未就绪',
    'wayland': '此应用的原生绘图通道尚未就绪',
}


class InputQueue:
    """Preserve clicks and key order while coalescing consecutive pointer updates."""

    def __init__(self, capacity=64):
        self.condition = threading.Condition()
        self.pending = deque()
        self.closed = False
        self.capacity = capacity

    def put(self, event):
        with self.condition:
            if self.closed:
                return
            if event[0] == 'move' and self.pending and self.pending[-1][0] == 'move':
                self.pending[-1] = event
            elif len(self.pending) < self.capacity:
                self.pending.append(event)
            elif event[0] == 'move':
                return
            else:
                raise RuntimeError('云端输入积压，请重新连接桌面。')
            self.condition.notify()

    def get(self):
        with self.condition:
            self.condition.wait_for(lambda: self.closed or self.pending)
            return None if self.closed else self.pending.popleft()

    def close(self):
        with self.condition:
            self.closed = True
            self.pending.clear()
            self.condition.notify_all()


def present_session(connect, replay, open_window, *, size=DEFAULT_SIZE):
    """Keep the Win32 message pump independent of the GPU, source and input ACKs."""
    width, height = checked_size(size)
    inputs = InputQueue()
    stop = threading.Event()
    finished = threading.Event()
    got_scene = threading.Event()
    lock = threading.Lock()
    peer = None
    errors = []

    def fail(exc):
        with lock:
            if not stop.is_set():
                errors.append(exc)
                stop.set()

    def submit(event):
        try:
            inputs.put(event)
        except RuntimeError as exc:
            fail(exc)

    window = open_window(f'{TITLE} · 正在连接云端桌面', width, height, submit)

    def send_input(session):
        try:
            while (event := inputs.get()) is not None:
                kind, *args = event
                if kind == 'super':
                    session.send_super()
                elif kind == 'move':
                    session.move(*args)
                elif kind == 'click':
                    session.click(*args)
                elif kind == 'key':
                    session.key(*args)
        except Exception as exc:
            fail(exc)

    def present(width, height, pixels):
        if stop.is_set():
            raise InterruptedError('窗口已关闭。')
        if (width, height) != size:
            raise ValueError('云端桌面的尺寸与当前会话不匹配。')
        if not window.present(pixels):
            raise InterruptedError('本机窗口已关闭。')
        got_scene.set()

    def close_peer():
        nonlocal peer
        with lock:
            session, peer = peer, None
        if session is not None:
            session.close()

    def display():
        nonlocal peer
        try:
            session = connect()
            with lock:
                peer = session
            if stop.is_set():
                return
            threading.Thread(target=send_input, args=(session,), name='gnome-input', daemon=True).start()
            replay(session, present)
            if not stop.is_set():
                fail(ConnectionError('云端桌面已断开。重新打开客户端可建立新会话。'))
        except Exception as exc:
            fail(exc)
        finally:
            inputs.close()
            try:
                close_peer()
            except Exception as exc:
                fail(exc)
            finally:
                finished.set()

    drawing = threading.Thread(target=display, name='gnome-graphics', daemon=True)
    drawing.start()
    has_title = False
    status_until = 0.0
    try:
        while window.pump() and not stop.is_set() and not finished.is_set():
            if not has_title and got_scene.is_set():
                window.set_title(f'{TITLE} · Ubuntu')
                has_title = True
            with lock:
                session = peer
            if has_title and session is not None:
                app = session.pop_unavailable()
                if app:
                    window.set_title(f'{TITLE} · {APPLICATION_STATUS[app]}')
                    status_until = time.monotonic() + 8
                elif status_until and time.monotonic() >= status_until:
                    window.set_title(f'{TITLE} · Ubuntu')
                    status_until = 0.0
            # Network I/O never runs on the window thread. Sleeping on the event
            # also wakes immediately on a decoder or input error.
            stop.wait(0.012)
    finally:
        stop.set()
        inputs.close()
        window.close()
        cleanup = threading.Thread(target=close_peer, name='gnome-close', daemon=True)
        cleanup.start()
        cleanup.join(2)
        drawing.join(2)
    if errors:
        raise errors[0]


def main(argv=None):
    parser = argparse.ArgumentParser(description='打开云端 GNOME 原生桌面。')
    parser.add_argument('--instabox', action='store_true', help='使用 InstaBox 私有通道')
    parser.add_argument('--port', type=int, default=17325)
    parser.add_argument('--machine')
    parser.add_argument('--size', choices=SIZE_NAMES, default='800x520')
    args = parser.parse_args(argv)
    if not 1 <= args.port <= 65535:
        parser.error('端口必须在 1 至 65535 之间。')
    if args.machine and not re.fullmatch(r'm_[A-Za-z0-9_]+', args.machine):
        parser.error('实例 ID 格式无效。')
    if sys.platform != 'win32':
        print('完整云桌面客户端需要 Windows。', file=sys.stderr)
        return 2
    from .viewer import hidden_value
    pairing = hidden_value('CLOUDWORKSPACE_GNOME_TOKEN', '云端桌面配对码（输入隐藏）：')
    if not pairing or not re.fullmatch(r'[A-Za-z0-9_-]{43}', pairing):
        print('请提供 GNOME 服务生成的配对码。', file=sys.stderr)
        return 2
    key = hidden_value('INSTABOX_API_KEY', 'InstaBox API 密钥（输入隐藏）：')
    if not key:
        print('请提供 InstaBox API 密钥。', file=sys.stderr)
        return 2
    try:
        from instabox import InstaBox
        from .gnome_transport import connect
        from .gnome_window import open_window
        from .virgl_windows import replay

        artwork = None

        def start():
            nonlocal artwork
            from cloud_render.gnome_wallpaper import decoded_asset
            try:
                artwork = decoded_asset(Path(__file__).with_name('vendor') / 'ubuntu-noble' / 'wallpaper.rgb.xz')
            except (OSError, ValueError, ImportError):
                artwork = None
            box = InstaBox(key, args.machine)
            box.ensure_running()
            return connect(box, args.port, pairing, size=SIZE_NAMES[args.size], wallpaper=artwork is not None)

        def render(session, present):
            replay(session, present, wallpaper=artwork)

        present_session(start, render, open_window, size=SIZE_NAMES[args.size])
    except BridgeError as exc:
        messages = {
            'network': '无法连接 InstaBox，请检查网络和系统时间。',
            'select_machine': '账户中有多台云机，请用 --machine 指定实例。',
            'machine_timeout': '云机启动超时，请查看实例状态后重连。',
            'machine_unavailable': '当前云机不可用，请检查实例状态。',
            'http_401': 'InstaBox 拒绝了 API 密钥，请检查后再连接。',
        }
        print(messages.get(exc.code, 'InstaBox 请求失败，请核对密钥和云机状态。'), file=sys.stderr)
        return 1
    except ImportError:
        print('客户端组件缺失，请重新解压完整的 Windows 客户端。', file=sys.stderr)
        return 1
    except (OSError, ValueError, RuntimeError) as exc:
        # The helpers below generate their messages locally. Avoid exposing
        # foreign exception text, which may contain URLs or service responses.
        from .instabox_transport import TransportError
        from .virgl_windows import VirglError
        if isinstance(exc, TransportError):
            message = '云桌面连接失败。请核对 GNOME 服务是否已启动，以及专用端口和桌面配对码。'
        elif isinstance(exc, VirglError):
            message = '本机绘图失败。请确认已完整解压 Windows 包，且系统显卡驱动支持 OpenGL。'
        elif isinstance(exc, ConnectionError):
            message = '云端桌面已断开。重新打开客户端可建立新会话。'
        else:
            message = '无法继续显示云桌面，请重新打开客户端。'
        print(message, file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

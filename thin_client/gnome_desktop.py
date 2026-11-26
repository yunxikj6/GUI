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


def present_session(connect, replay, open_window, *, size=DEFAULT_SIZE, retain_on_disconnect=False, restore=None):
    """Keep the Win32 message pump independent of the GPU, source and input ACKs."""
    width, height = checked_size(size)
    inputs = InputQueue()
    stop = threading.Event()
    finished = threading.Event()
    got_scene = threading.Event()
    window_closed = threading.Event()
    lock = threading.Lock()
    disconnect_lock = threading.Lock()
    peer = None
    errors = []

    def fail(exc):
        with lock:
            if not stop.is_set():
                errors.append(exc)
                stop.set()
            else:
                return
        inputs.close()
        # When the window remains open the input or GPU thread may still be
        # waiting on the other half of this connection. Close the transport once.
        threading.Thread(target=close_failed_peer, name='gnome-detach', daemon=True).start()

    def close_failed_peer():
        try:
            close_peer()
        except Exception:
            # The primary fault already stopped the input queue and renderer.
            pass

    def submit(event):
        try:
            inputs.put(event)
        except RuntimeError as exc:
            fail(exc)

    window = open_window(f'{TITLE} · 正在连接云端桌面', width, height, submit)

    def current_text_focus():
        with lock:
            session = peer
        return session.text_focus() if session is not None and not stop.is_set() else None

    window.text_focus = current_text_focus

    def send_input(session):
        try:
            while not stop.is_set() and (event := inputs.get()) is not None:
                if stop.is_set():
                    break
                kind, *args = event
                if kind == 'super':
                    session.send_super()
                elif kind == 'move':
                    session.move(*args)
                elif kind == 'click':
                    session.click(*args)
                elif kind == 'key':
                    session.key(*args)
                elif kind == 'delete-key':
                    revision, backward = args
                    if session.has_text_focus_protocol():
                        if revision is not None:
                            session.delete_text(revision, backward=backward)
                    else:
                        session.key('Backspace' if backward else 'Delete')
                elif kind == 'text':
                    session.commit_text(*args)
                elif kind == 'select-all':
                    session.select_all(*args)
        except Exception as exc:
            fail(exc)

    def present(width, height, pixels):
        with lock:
            if stop.is_set():
                raise InterruptedError('窗口已关闭。')
            if (width, height) != size:
                raise ValueError('云端桌面的尺寸与当前会话不匹配。')
            if not window.present(pixels):
                raise InterruptedError('本机窗口已关闭。')
            got_scene.set()

    def present_restored(w, h, pixels):
        with lock:
            if window_closed.is_set():
                raise InterruptedError('窗口已关闭。')
            if (w, h) != size:
                raise ValueError('本机重新绘制的桌面尺寸不符。')
            window.present(pixels)

    def close_peer():
        nonlocal peer
        with disconnect_lock:
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
            try:
                if restore and got_scene.is_set() and not window_closed.is_set():
                    # A fresh WGL context replays only the authenticated prefix.
                    # Both network directions and the input queue have been closed.
                    restore(present_restored, window_closed.is_set)
            except InterruptedError:
                if not window_closed.is_set():
                    with lock:
                        errors.append(ValueError('本机重新绘制被中断。'))
            except Exception as exc:
                with lock:
                    errors.append(exc)
            finally:
                finished.set()

    drawing = threading.Thread(target=display, name='gnome-graphics', daemon=True)
    drawing.start()
    has_title = False
    status_until = 0.0
    retained = False
    readonly_title = None
    try:
        while window.pump():
            if stop.is_set() or finished.is_set():
                if not retain_on_disconnect or not got_scene.is_set():
                    break
                with lock:
                    invalid = any(isinstance(error, ValueError) for error in errors)
                reason = '绘图或输入未通过校验' if invalid else '连接已断开'
                if reason != readonly_title:
                    window.set_title(f'{TITLE} · {reason} · 只读')
                    readonly_title = reason
                retained = True
                # Native WM_PAINT/resize may redraw the last locally completed
                # buffer. No network read or input submission is available now.
                time.sleep(.025)
                continue
            if not has_title and got_scene.is_set():
                window.set_title(f'{TITLE} · Ubuntu')
                has_title = True
            with lock:
                session = peer
            if has_title and session is not None:
                app = session.pop_unavailable()
                take_notice = getattr(session, 'pop_input_notice', None)
                notice = take_notice() if take_notice else None
                if notice:
                    window.set_title(f'{TITLE} · {notice}')
                    status_until = time.monotonic() + 8
                elif app:
                    window.set_title(f'{TITLE} · {APPLICATION_STATUS[app]}')
                    status_until = time.monotonic() + 8
                elif status_until and time.monotonic() >= status_until:
                    window.set_title(f'{TITLE} · Ubuntu')
                    status_until = 0.0
            # Network I/O never runs on the window thread. Sleeping on the event
            # also wakes immediately on a decoder or input error.
            stop.wait(0.012)
    finally:
        window_closed.set()
        stop.set()
        inputs.close()
        window.close()
        cleanup = threading.Thread(target=close_peer, name='gnome-close', daemon=True)
        cleanup.start()
        cleanup.join(2)
        drawing.join(2)
    for error in errors:
        if not retained or isinstance(error, ValueError) or not isinstance(error, OSError):
            raise error


def main(argv=None):
    parser = argparse.ArgumentParser(description='打开云端 GNOME 原生桌面。')
    route = parser.add_mutually_exclusive_group()
    route.add_argument('--instabox', action='store_true', help='使用 InstaBox 私有通道')
    route.add_argument('--forwarded', action='store_true', help='使用已转发至本机回环的 GNOME 服务端口')
    parser.add_argument('--port', type=int, default=17325)
    parser.add_argument('--machine')
    parser.add_argument('--size', choices=SIZE_NAMES, default='800x520')
    args = parser.parse_args(argv)
    if not 1 <= args.port <= 65535:
        parser.error('端口必须在 1 至 65535 之间。')
    if args.forwarded and args.machine is not None:
        parser.error('已转发的 GNOME 服务不能与 --machine 同时指定。')
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
    if not args.forwarded:
        key = hidden_value('INSTABOX_API_KEY', 'InstaBox API 密钥（输入隐藏）：')
        if not key:
            print('请提供 InstaBox API 密钥。', file=sys.stderr)
            return 2
    try:
        from instabox import InstaBox
        from .gnome_transport import connect, connect_forwarded
        from .gnome_window import open_window
        from .virgl_windows import replay
        from .scene_cache import SceneCache

        artwork = None

        def start():
            nonlocal artwork
            from cloud_render.gnome_wallpaper import decoded_asset
            try:
                artwork = decoded_asset(Path(__file__).with_name('vendor') / 'ubuntu-noble' / 'wallpaper.rgb.xz')
            except (OSError, ValueError, ImportError):
                artwork = None
            if args.forwarded:
                return connect_forwarded(args.port, pairing, size=SIZE_NAMES[args.size], wallpaper=artwork is not None, search_input=True, focus_delete=True)
            box = InstaBox(key, args.machine)
            box.ensure_running()
            return connect(box, args.port, pairing, size=SIZE_NAMES[args.size], wallpaper=artwork is not None, search_input=True, focus_delete=True)

        cache = None

        def draw(stream, present):
            replay(stream, present, wallpaper=artwork)

        def render(session, present):
            nonlocal cache
            cache = SceneCache(session)

            def capture(w, h, pixels):
                present(w, h, pixels)
                cache.presented(w, h, pixels)

            draw(cache, capture)

        def restore(present, cancelled):
            if cache is not None:
                cache.redraw(draw, present, cancelled)

        present_session(start, render, open_window, size=SIZE_NAMES[args.size],
                        retain_on_disconnect=True, restore=restore)
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

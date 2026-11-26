"""Open the paired Ubuntu session in a native Win64 window with original app painting."""
import argparse
from pathlib import Path
import re
import sys
import threading

from experiments.live_application_surface import LiveSurfaceStream
from experiments.original_desktop_protocol import SIZE, read_hello
from experiments.persistent_application_painter import OriginalPicturePainter


class _WindowInput:
    """A down/up pair in a waiting or subsequently changed view cannot change focus."""
    def __init__(self):
        self.lock = threading.Lock()
        self.reader = None
        self.visible = None
        self.last_binding = None
        self.notice = None

    def presented(self, stream):
        with self.lock:
            if not stream.can_present:
                self.visible = None
                return False
            self.last_binding = stream.binding
            self.visible = (stream.binding, stream.digest)
            return True

    def ticket(self):
        with self.lock:
            if (self.reader is not None and self.reader.can_present and
                    (self.reader.binding, self.reader.digest) == self.visible):
                return self.visible

    def rejected_click(self):
        with self.lock:
            self.notice = '画面正在更新，刚才的点击未发送。请确认画面后再点击。'

    def rejected_edit(self):
        with self.lock:
            self.notice = '原厂输入焦点已更改，请在当前输入框继续。'

    def pop_input_notice(self):
        with self.lock:
            notice, self.notice = self.notice, None
            return notice

    def has_displayed_application(self):
        # During an animation the last verified window can stay on screen.
        # The source's focus epoch independently binds editing to its control.
        with self.lock:
            return (self.reader is not None and self.last_binding is not None and
                    self.reader.binding == self.last_binding)

    def events(self, submit):
        def handle(event):
            if event[0] == 'move':
                submit(event)
            elif event[0] in ('text', 'select-all', 'delete-key'):
                if self.has_displayed_application():
                    submit(event)
                else:
                    self.rejected_edit()
            elif event[0] == 'click' and event[3] == 'left':
                original = self.ticket()
                if original is not None:
                    submit((*event, original))
                else:
                    self.rejected_click()
        return handle


class _PointerDesktop:
    def __init__(self, transport, state):
        self.transport, self.state = transport, state

    def click(self, x, y, button, displayed):
        if self.state.ticket() == displayed:
            self.transport.click(x, y, button)
        else:
            self.state.rejected_click()

    def move(self, x, y):
        self.transport.move(x, y)

    def has_application_wallpaper(self):
        return self.transport.has_application_wallpaper()

    def read(self, size):
        return self.transport.read(size)

    def text_focus(self):
        if self.state.has_displayed_application():
            return self.transport.text_focus()
        return None

    def commit_text(self, revision, text):
        if self.text_focus() == revision and self.transport.commit_text(revision, text):
            return True
        self.state.rejected_edit()
        return False

    def select_all(self, revision):
        if self.text_focus() == revision and self.transport.select_all(revision):
            return True
        self.state.rejected_edit()
        return False

    def has_text_focus_protocol(self):
        # This window has no unscoped Delete/Backspace compatibility path.
        return True

    def delete_text(self, revision, *, backward):
        if self.text_focus() == revision and self.transport.delete_text(revision, backward=backward):
            return True
        self.state.rejected_edit()
        return False

    def pop_input_notice(self):
        return self.state.pop_input_notice()

    def pop_unavailable(self):
        return self.transport.pop_unavailable()

    def close(self):
        self.transport.close()


def present_original_desktop(connect, open_window, *, node='node', runtime_dir=None, artwork=None):
    """Use the standard Win32 message loop, GPU thread and acknowledged input queue."""
    from thin_client.gnome_desktop import present_session
    from thin_client.scene_cache import SceneCache
    from thin_client.virgl_windows import replay
    state = _WindowInput()
    cache = None
    session_wallpaper = None

    def native_window(title, width, height, submit):
        return open_window(title, width, height, state.events(submit))

    def draw(connection, present, *, live):
        nonce, exe = read_hello(connection)

        def authorized(binding):
            # The paired bridge binds this stream to its observed Linux process and Wayland surface.
            return binding.session == nonce and binding.executable_sha256 == exe

        with OriginalPicturePainter(node=node) as painter:
            source = LiveSurfaceStream(connection, session=nonce, authorize=authorized, rasterize=painter,
                                       wallpaper=session_wallpaper)
            if live:
                state.reader = source

            def display(width, height, pixels):
                allowed = state.presented(source) if live else source.can_present
                if allowed:
                    present(width, height, pixels)
                    if live:
                        cache.presented(width, height, pixels)

            try:
                replay(source, display, runtime_dir=runtime_dir)
            finally:
                if live:
                    with state.lock:
                        state.reader = None

    def render(connection, present):
        nonlocal cache, session_wallpaper
        if artwork is not None and connection.has_application_wallpaper():
            session_wallpaper = artwork
        # Cache the source records, before the local engine expands SkPictures into textures.
        cache = SceneCache(connection)
        draw(cache, present, live=True)

    def restore(present, cancelled):
        if cache is not None:
            cache.redraw(lambda stream, emit: draw(stream, emit, live=False), present, cancelled)

    present_session(lambda: _PointerDesktop(connect(), state), render, native_window,
                    size=SIZE, retain_on_disconnect=True, restore=restore)


def main(argv=None):
    parser = argparse.ArgumentParser(description='打开云端 Ubuntu 和原厂应用的本地 Win32 窗口。')
    route = parser.add_mutually_exclusive_group()
    route.add_argument('--instabox', action='store_true', help='通过 InstaBox 官方私有通道连接云源')
    route.add_argument('--forwarded', action='store_true', help='通过已转发的本机回环端口连接云源')
    parser.add_argument('--port', type=int, default=17326, help='云源私有端口或本机已转发端口')
    parser.add_argument('--machine', help='目标 InstaBox 实例')
    parser.add_argument('--node', type=Path, help='本机 Windows Node.js 可执行文件')
    parser.add_argument('--runtime-dir', type=Path, help='Win64 VirGL 依赖目录')
    args = parser.parse_args(argv)
    if sys.platform != 'win32':
        parser.error('原生显示端需要 x64 Windows。')
    if not 0 < args.port <= 65535 or (args.machine and
            (not args.instabox or not re.fullmatch(r'm_[A-Za-z0-9_]+', args.machine))):
        parser.error('请核对端口与 InstaBox 实例。')
    from thin_client.viewer import hidden_value
    from thin_client.gnome_window import open_window
    from thin_client.gnome_transport import connect, connect_forwarded

    token = hidden_value('CLOUDWORKSPACE_GNOME_TOKEN', '云桌面配对码（输入隐藏）：')
    if not token or not re.fullmatch(r'[A-Za-z0-9_-]{43}',token):
        parser.error('配对码格式不正确。')
    from cloud_render.gnome_wallpaper import decoded_asset
    try:
        artwork = decoded_asset(Path(__file__).resolve().parents[1] / 'thin_client/vendor/ubuntu-noble/wallpaper.rgb.xz')
    except (OSError, ValueError, ImportError):
        artwork = None
    if args.instabox:
        from instabox import InstaBox
        key = hidden_value('INSTABOX_API_KEY', 'InstaBox API 密钥（输入隐藏）：')
        if not key:
            parser.error('请提供 InstaBox API 密钥。')
        def start():
            box = InstaBox(key, args.machine)
            box.ensure_running()
            return connect(box,args.port,token, size=SIZE, search_input=True, focus_delete=True,
                           app_wallpaper=artwork is not None)
    else:
        def start():
            return connect_forwarded(args.port,token, size=SIZE, search_input=True, focus_delete=True,
                           app_wallpaper=artwork is not None)

    bundled = Path(__file__).resolve().parents[1]/'thin_client/vendor/node/windows-x64/node.exe'
    node = args.node or (bundled if bundled.is_file() else 'node')
    try:
        present_original_desktop(start, open_window, node=node, runtime_dir=args.runtime_dir, artwork=artwork)
    except (ValueError, OSError, RuntimeError) as exc:
        detail = '请确认云端交互源、配对码、私有连接与客户端图形组件。'
        print(f'原厂桌面连接已结束（{type(exc).__name__}）。{detail}',file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

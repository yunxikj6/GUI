"""Paint the verified cloud system-browser viewport in a native Win32 window."""
import argparse
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from pathlib import Path
import re
import socket
import sys
import time

from cloud_render.wire import Click, KeyPress
from experiments.persistent_application_painter import OriginalPicturePainter
from thin_client.viewer import AppliedWithoutScene, Disconnected, DisplaySession, Stale, hidden_value


_KEYS = {'Up': 'ArrowUp', 'Down': 'ArrowDown', 'PageUp': 'PageUp', 'PageDown': 'PageDown',
         'Home': 'Home', 'End': 'End'}
_TITLE = 'Chromium · 云端视口'


def _pixels(painter, frame):
    rgba = painter(frame)
    bgrx = bytearray(rgba)
    bgrx[0::4], bgrx[2::4] = rgba[2::4], rgba[0::4]
    return bgrx


def present_viewport(session, open_window, painter):
    """Keep Windows responsive while Chromium, the network, or CanvasKit is busy."""
    presented = submitted = 0
    window = action = drawing = None
    with ThreadPoolExecutor(max_workers=2) as workers:
        pressed = None

        def input_event(event):
            nonlocal action, pressed
            if event[0] in ('pointer-cancel', 'viewport-changing'):
                pressed = None
                return
            with session.changed:
                ready = not session.disconnected and presented and session.frame.sequence == presented
            if not ready or action is not None:
                pressed = None
                return
            if event[0] == 'press':
                pressed = (presented, *event[1:])
                return
            if len(event) == 4 and event[0] == 'click' and event[3] == 'left':
                pair, pressed = pressed, None
                if pair != (presented, event[1], event[2]):
                    return
                message = Click(*pair)
            elif len(event) == 2 and event[0] == 'key' and event[1] in _KEYS:
                message = KeyPress(presented, _KEYS[event[1]])
            else:
                return
            action = workers.submit(session.send_input, message, wait_for_scene=True)

        def painted(sequence):
            nonlocal presented
            presented = sequence

        try:
            while window is None or window.pump():
                with session.changed:
                    if session.disconnected:
                        raise Disconnected()
                    latest = session.frame
                if action is not None and action.done():
                    try:
                        action.result()
                    except Stale:
                        pass
                    action = None
                if drawing is not None and drawing.done():
                    pixels = drawing.result()
                    drawing = None
                    if window is None or (window.width, window.height) != (painting.width, painting.height):
                        if window is not None:
                            window.close()
                        window = open_window(_TITLE, painting.width, painting.height, input_event)
                        window.track_button_pair = True
                    presented = 0
                    pressed = None
                    if not window.present(pixels, on_painted=lambda seq=painting.sequence: painted(seq)):
                        return
                if drawing is None and latest.sequence > submitted:
                    # Retain at most one in-flight CanvasKit operation and the source's newest frame.
                    painting = latest
                    submitted = painting.sequence
                    drawing = workers.submit(_pixels, painter, painting)
                time.sleep(.012)
        finally:
            if window is not None:
                window.close()
            session.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description='在原生 Windows 窗口中重绘云端 Chromium 视口。')
    parser.add_argument('--port', type=int, default=17328)
    parser.add_argument('--instabox', action='store_true', help='使用 InstaBox 私有通道')
    parser.add_argument('--machine', help='目标 InstaBox 实例')
    parser.add_argument('--node', type=Path, help='本机 Windows Node.js 可执行文件')
    args = parser.parse_args(argv)
    if sys.platform != 'win32':
        parser.error('此入口需要 x64 Windows。')
    if not 0 < args.port < 65536 or (args.machine and
            (not args.instabox or not re.fullmatch(r'm_[A-Za-z0-9_]+', args.machine))):
        parser.error('请核对端口和 InstaBox 实例。')
    pairing = hidden_value('CLOUDWORKSPACE_RENDER_TOKEN', '云端 Chromium 视口配对码（输入隐藏）：')
    if not pairing or not re.fullmatch(r'[A-Za-z0-9_-]{43}', pairing):
        parser.error('需要有效的视口配对码。')
    from thin_client.gnome_window import open_window
    from instabox import BridgeError, InstaBox

    bundle = Path(__file__).resolve().parents[1] / 'thin_client/vendor/node/windows-x64/node.exe'
    try:
        with ExitStack() as resources:
            if args.instabox:
                from thin_client.instabox_transport import connect
                key = hidden_value('INSTABOX_API_KEY', 'InstaBox API 密钥（输入隐藏）：')
                if not key:
                    parser.error('请输入 InstaBox API 密钥。')
                box = InstaBox(key, args.machine)
                box.ensure_running()
                peer, transport = connect(box, args.port, pairing)
                resources.callback(transport.close)
            else:
                peer = socket.create_connection(('127.0.0.1', args.port), timeout=45)
            resources.callback(peer.close)
            if not args.instabox:
                peer.sendall(b'CLRP-AUTH/1 ' + pairing.encode('ascii') + b'\n')
            session = DisplaySession(peer)
            resources.callback(session.close)
            painter = resources.enter_context(OriginalPicturePainter(node=args.node or bundle))
            present_viewport(session, open_window, painter)
    except (AppliedWithoutScene, BridgeError, Disconnected, OSError, RuntimeError, ValueError) as exc:
        print(f'Chromium 云端视口已关闭（{type(exc).__name__}）。', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

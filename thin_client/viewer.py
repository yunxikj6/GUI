"""Display an authorized cloud rendering stream through a local, in-memory viewer."""
from __future__ import annotations

import argparse
from collections import deque
from contextlib import ExitStack
import getpass
import hmac
import io
import json
import os
import re
import secrets
import socket
import sys
import threading
import time
import warnings
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from cloud_render.wire import Click, Frame, InputAck, KeyPress, TextCommit, WireError, encode_message, read_message, write_message


ASSETS = Path(__file__).resolve().parent
FILES = {
    '/': ('index.html', 'text/html; charset=utf-8'),
    '/viewer.css': ('viewer.css', 'text/css; charset=utf-8'),
    '/viewer.js': ('viewer.js', 'text/javascript; charset=utf-8'),
    '/input.js': ('input.js', 'text/javascript; charset=utf-8'),
    '/renderer.js': ('renderer.js', 'text/javascript; charset=utf-8'),
    '/vendor/canvaskit.js': ('vendor/canvaskit.js', 'text/javascript; charset=utf-8'),
    '/vendor/canvaskit.wasm.gz': ('vendor/canvaskit.wasm.gz', 'application/octet-stream'),
}
CLICK_BYTES = 24  # 12-byte CLRP v1 envelope followed by !Iff
MAX_INPUT_JSON = 32 * 1024  # Allows JSON escaping of a full UTF-8 text commit.


class Disconnected(Exception):
    pass


class Stale(Exception):
    pass


class AppliedWithoutScene(Exception):
    pass


def keyboard_message(raw):
    def fields(pairs):
        result = dict(pairs)
        if len(result) != len(pairs):
            raise WireError('duplicate field')
        return result

    body = json.loads(raw.decode('utf-8'), object_pairs_hook=fields)
    if type(body) is dict:
        if body.get('type') == 'key' and body.keys() == {'type', 'sequence', 'key', 'modifiers'}:
            return KeyPress(body['sequence'], body['key'], body['modifiers'])
        if body.get('type') == 'text' and body.keys() == {'type', 'sequence', 'text'}:
            return TextCommit(body['sequence'], body['text'])
    raise WireError('invalid input fields')


class DisplaySession:
    def __init__(self, peer):
        self.peer = peer
        self.stream = peer.makefile('rwb')
        self.changed = threading.Condition()
        self.disconnected = False
        self.pending = None
        self.ack = None
        self.following = None
        self.consumed = 0
        try:
            self.frame = read_message(self.stream, expected=Frame)
            if self.frame is None:
                raise Disconnected()
            # Store contiguous ranges rather than individual pictures. An editor
            # can animate for minutes without changing the original IME target.
            self.seen = deque([(self.frame.sequence, self.frame.sequence, self.frame.width, self.frame.height)], maxlen=256)
            self.encoded = encode_message(self.frame)
            peer.settimeout(None)
            self.reader = threading.Thread(target=self._read_updates, daemon=True)
            self.reader.start()
        except BaseException:
            self.close()
            raise

    def _read_updates(self):
        try:
            while True:
                message = read_message(self.stream, expected=(Frame, InputAck))
                if message is None:
                    break
                with self.changed:
                    if isinstance(message, Frame):
                        if message.sequence < self.frame.sequence:
                            raise WireError('out of order frame')
                        if message.sequence == self.frame.sequence:
                            if message != self.frame:
                                raise WireError('conflicting frame sequence')
                            continue
                        self.encoded = encode_message(message)
                        self.frame = message
                        size = message.width, message.height
                        if self.seen and self.seen[-1][1] + 1 == message.sequence and self.seen[-1][2:] == size:
                            self.seen[-1] = (self.seen[-1][0], message.sequence, *size)
                        else:
                            self.seen.append((message.sequence, message.sequence, *size))
                        if self.ack is True and self.following is None:
                            self.following = message.sequence
                    else:
                        if self.pending != message.sequence or self.ack is not None:
                            raise WireError('unexpected acknowledgement')
                        self.ack = message.applied
                        self.consumed = self.frame.sequence
                        self.seen.clear()
                    self.changed.notify_all()
        except (OSError, EOFError, WireError):
            pass
        finally:
            self.close()

    def snapshot(self, after=None):
        with self.changed:
            if after is not None:
                if after > self.frame.sequence:
                    raise WireError('unknown frame sequence')
                self.changed.wait_for(lambda: self.frame.sequence > after or self.disconnected, timeout=10)
                if self.frame.sequence == after:
                    if self.disconnected:
                        raise Disconnected()
                    return None
            return self.encoded

    def send_input(self, event, *, wait_for_scene=False):
        with self.changed:
            if self.disconnected:
                raise Disconnected()
            # An IME confirmation stays attached to its originally shown frame.
            # Only the cloud can verify that the original edit target and
            # selection survived paint-only updates throughout composition.
            known = event.sequence == self.frame.sequence
            if isinstance(event, TextCommit) and not known:
                known = any(first <= event.sequence <= last for first, last, _, _ in self.seen)
            if isinstance(event, Click) and not known and self.seen:
                # The full interval must preserve geometry, including all
                # intermediate paints. The cloud verifies the original hit.
                first, last, _, _ = self.seen[-1]
                known = first <= event.sequence <= last
            if self.pending is not None or not known or event.sequence <= self.consumed:
                raise Stale()
            if isinstance(event, Click) and (event.x >= self.frame.width or event.y >= self.frame.height):
                raise WireError('click outside the original viewport')
            self.pending = event.sequence
            self.ack = None
            self.following = None
            try:
                write_message(self.stream, event)
                self.changed.wait_for(lambda: self.ack is not None or self.disconnected, timeout=45)
                if self.ack is None:
                    raise Disconnected()
                if not self.ack:
                    raise Stale()
                if not isinstance(event, Click) or wait_for_scene:
                    self.changed.wait_for(lambda: self.following is not None or self.disconnected, timeout=30)
                    if self.following is None:
                        self.close()
                        raise AppliedWithoutScene()
                    return self.following
            except (OSError, EOFError, Disconnected):
                # An input may have taken effect even if the acknowledgement was lost.
                self.close()
                raise Disconnected() from None
            finally:
                self.pending = None
                self.ack = None
                self.following = None

    def close(self):
        with self.changed:
            if self.disconnected:
                return
            self.disconnected = True
            self.changed.notify_all()
        # Wake a background reader before closing its buffered stream.
        try:
            self.peer.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            self.stream.close()
        except OSError:
            pass
        finally:
            self.peer.close()


class ViewerServer(ThreadingHTTPServer):
    def __init__(self, session):
        self.session = session
        self.token = secrets.token_urlsafe(32)
        super().__init__(('127.0.0.1', 0), ViewerHandler)
        self.authority = '127.0.0.1:' + str(self.server_port)
        self.origin = 'http://' + self.authority
        self.tab_lock = threading.Lock()
        self.tabs = {}
        self.opened = False
        self.exit_after = time.monotonic() + 90

    def touch_tab(self, tab, *, leaving=False):
        if not re.fullmatch(r'[a-f0-9]{32}', tab):
            return
        now = time.monotonic()
        with self.tab_lock:
            if leaving:
                self.tabs.pop(tab, None)
            else:
                self.tabs[tab] = now
                self.opened = True
            # A page may be frozen in the background. An explicit last-tab
            # close exits promptly; the longer deadline catches browser crashes.
            self.exit_after = max(self.tabs.values()) + 900 if self.tabs else now + 10

    def serve_browser(self):
        ended = threading.Event()
        def launch():
            try:
                if not open_local_viewer(self.url, on_closed=ended.set):
                    ended.set()
            except OSError:
                ended.set()

        # Some configured browser commands return only when their window exits.
        # The application must already be serving its page during that call.
        threading.Thread(target=launch, daemon=True).start()
        self.timeout = 1
        while not ended.is_set():
            with self.tab_lock:
                if time.monotonic() >= self.exit_after:
                    break
            self.handle_request()
        return self.opened

    @property
    def url(self):
        # The fragment stays in the browser and never appears in HTTP request paths.
        return self.origin + '/#' + self.token


class ViewerHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass

    def setup(self):
        super().setup()
        self.connection.settimeout(15)

    def send_data(self, code, data=b'', mime='text/plain; charset=utf-8', *, next_scene=None):
        self.send_response(code)
        self.send_header('Content-Type', mime)
        self.send_header('Content-Length', str(len(data)))
        self.send_header('Cache-Control', 'no-store')
        if next_scene is not None:
            self.send_header('X-Next-Scene', str(next_scene))
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Referrer-Policy', 'no-referrer')
        self.send_header('Content-Security-Policy', "default-src 'none'; script-src 'self' 'wasm-unsafe-eval'; style-src 'self'; connect-src 'self' blob:; frame-ancestors 'none'; base-uri 'none'; form-action 'none'")
        self.end_headers()
        try:
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def allowed(self, *, private=False):
        origins = self.headers.get_all('Origin') or []
        hosts = self.headers.get_all('Host') or []
        if (hosts != [self.server.authority] or self.headers.get('Sec-Fetch-Site') == 'cross-site' or
                (origins and origins != [self.server.origin]) or (self.command == 'POST' and not origins)):
            self.send_data(403)
            return False
        if private:
            value = self.headers.get('X-Viewer-Token', '')
            if not value.isascii() or not hmac.compare_digest(value, self.server.token):
                self.send_data(403)
                return False
        return True

    def do_GET(self):
        path, _, query = self.path.partition('?')
        if not self.allowed(private=path == '/frame'):
            return
        if path == '/frame':
            self.server.touch_tab(self.headers.get('X-Viewer-Tab', ''))
            after = None
            if query:
                match = re.fullmatch(r'after=([1-9][0-9]{0,9})', query)
                if not match:
                    self.send_data(400)
                    return
                after = int(match.group(1))
            try:
                data = self.server.session.snapshot(after)
            except Disconnected:
                self.send_data(503)
                return
            except WireError:
                self.send_data(400)
                return
            self.send_data(204 if data is None else 200, data or b'', 'application/octet-stream')
        elif self.path in FILES:
            name, mime = FILES[self.path]
            try:
                data = (ASSETS / name).read_bytes()
            except OSError:
                self.send_data(503)
                return
            self.send_data(200, data, mime)
        else:
            self.send_data(404)

    def do_POST(self):
        if not self.allowed(private=True):
            return
        if self.path == '/leave':
            self.server.touch_tab(self.headers.get('X-Viewer-Tab', ''), leaving=True)
            self.send_data(204)
            return
        if self.path not in ('/click', '/input'):
            self.send_data(404)
            return
        lengths = self.headers.get_all('Content-Length') or []
        if (len(lengths) != 1 or not re.fullmatch(r'[1-9][0-9]{0,4}', lengths[0]) or
                self.headers.get('Transfer-Encoding')):
            self.send_data(400)
            return
        size = int(lengths[0])
        mime = self.headers.get_content_type()
        try:
            if self.path == '/click' and size == CLICK_BYTES and mime == 'application/octet-stream':
                event = read_message(io.BytesIO(self.rfile.read(size)), expected=Click)
                if event is None:
                    raise WireError('missing click')
            elif self.path == '/input' and size <= MAX_INPUT_JSON and mime == 'application/json':
                raw = self.rfile.read(size)
                if len(raw) != size:
                    raise WireError('truncated input')
                event = keyboard_message(raw)
            else:
                raise WireError('invalid input size or media type')
            following = self.server.session.send_input(event, wait_for_scene=self.headers.get('X-Wait-For-Scene') == '1')
        except Stale:
            self.send_data(409)
        except (ValueError, TypeError, RecursionError, OSError, EOFError):
            self.send_data(400)
        except Disconnected:
            self.send_data(503)
        except AppliedWithoutScene:
            self.send_data(202)
        else:
            self.send_data(204, next_scene=following)


def open_local_viewer(url, on_closed=None):
    from thin_client.app_window import open_app_window
    return open_app_window(url, on_closed)


def hidden_value(name, prompt):
    value = os.environ.pop(name, None)
    if value or sys.stdin is None or not sys.stdin.isatty():
        return value
    try:
        with warnings.catch_warnings():
            warnings.simplefilter('error', getpass.GetPassWarning)
            return getpass.getpass(prompt)
    except (getpass.GetPassWarning, EOFError, KeyboardInterrupt):
        return None


def main(argv=None):
    from instabox import BridgeError, InstaBox
    parser = argparse.ArgumentParser(description='在本机重绘云端应用的绘图指令。')
    parser.add_argument('--port', type=int, required=True, help='本机隧道端口；使用 --instabox 时为云机绘图服务的回环端口')
    parser.add_argument('--instabox', action='store_true', help='通过 InstaBox 私有 HTTPS 通道连接云端绘图服务')
    parser.add_argument('--machine', help='InstaBox 实例 ID（账户中有多台云机时使用）')
    parser.add_argument('--no-browser', action='store_true', help='打印本机查看地址，手动打开浏览器')
    args = parser.parse_args(argv)
    if not 1 <= args.port <= 65535:
        parser.error('端口应在 1 到 65535 之间。')
    if args.machine and not args.instabox:
        parser.error('--machine 需要和 --instabox 一起使用。')
    pairing = hidden_value('CLOUDWORKSPACE_RENDER_TOKEN', '云端绘图会话配对码（输入隐藏）：')
    if not pairing or not re.fullmatch(r'[A-Za-z0-9_-]{43}', pairing):
        print('需要云端绘图服务生成的配对码。请在终端输入，或设置 CLOUDWORKSPACE_RENDER_TOKEN。', file=sys.stderr)
        return 1
    with ExitStack() as cleanup:
        try:
            if args.instabox:
                from .instabox_transport import connect
                key = hidden_value('INSTABOX_API_KEY', 'InstaBox API 密钥（输入隐藏）：')
                if not key:
                    print('需要 InstaBox API 密钥。请在终端输入，或设置 INSTABOX_API_KEY。', file=sys.stderr)
                    return 1
                box = InstaBox(key, args.machine)
                box.ensure_running()
                peer, transport = connect(box, args.port, pairing)
                cleanup.callback(transport.close)
            else:
                peer = socket.create_connection(('127.0.0.1', args.port), timeout=45)
            cleanup.callback(peer.close)
            if not args.instabox:
                peer.sendall(b'CLRP-AUTH/1 ' + pairing.encode('ascii') + b'\n')
            session = DisplaySession(peer)
            cleanup.callback(session.close)
        except BridgeError as exc:
            print('无法连接所选 InstaBox 云机。' + str(exc), file=sys.stderr)
            return 1
        except (OSError, WireError, EOFError, Disconnected):
            print('未收到云端绘图服务的有效首帧。请检查配对码、绘图服务与连接端口。', file=sys.stderr)
            return 1
        try:
            with ViewerServer(session) as server:
                if args.no_browser:
                    print(server.url, flush=True)
                    server.serve_forever()
                elif not server.serve_browser():
                    print('无法打开本机浏览器。可从终端用 --no-browser 获取查看地址。', file=sys.stderr)
                    return 1
        except KeyboardInterrupt:
            pass
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

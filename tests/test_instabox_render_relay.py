import base64
from concurrent.futures import Future, ThreadPoolExecutor
import hashlib
import json
from pathlib import Path
import secrets
import socket
import stat
import subprocess
import sys
import threading
import time
import unittest

from cloud_render import instabox_relay as relay
from cloud_render.wire import Click, ClickAck, Frame, Layer, encode_message, read_message


SOURCE = Path(relay.__file__).read_text(encoding='utf-8')


def frame(seq, size):
    return encode_message(Frame(seq, 120, 90, (Layer(0, 0, 120, 90, b'skiapict' + b'x' * size),)))


@unittest.skipUnless(sys.platform.startswith('linux'), 'cloud helper requires Linux')
class ProviderRelay(unittest.TestCase):
    def setUp(self):
        self.session = secrets.token_hex(16)
        self.root = relay.session_dir(self.session)
        self.token = secrets.token_urlsafe(32)
        self.listener = socket.create_server(('127.0.0.1', 0))
        self.listener.settimeout(8)
        self.port = self.listener.getsockname()[1]
        self.addCleanup(self.listener.close)
        self.addCleanup(self.rpc, 'close')

    def rpc(self, op, *, source=SOURCE, **params):
        result = subprocess.run([sys.executable, '-I', '-B', '-c', source], capture_output=True, timeout=12,
                                input=json.dumps({'op': op, 'session': self.session, **params}), text=True, check=True)
        self.assertFalse(result.stderr)
        return json.loads(result.stdout)

    def upstream(self, run):
        finished = Future()

        def serve():
            try:
                with self.listener.accept()[0] as sock, sock.makefile('rb') as reader:
                    sock.settimeout(8)
                    self.assertEqual(reader.readline(), ('CLRP-AUTH/1 ' + self.token + '\n').encode())
                    run(sock, reader)
                finished.set_result(True)
            except BaseException as exc:
                finished.set_exception(exc)
        thread = threading.Thread(target=serve, daemon=True)
        thread.start()
        return finished

    def test_complete_frames_stable_file_retries_click_once_ordered_close(self):
        large = frame(1, 2 * 1024 * 1024)
        refreshed = frame(2, 64)
        click = Click(1, 78, 32)
        ack = encode_message(ClickAck(1, True))
        done = threading.Event()
        delivered = threading.Event()

        def serve(sock, reader):
            sock.sendall(large)
            self.assertEqual(read_message(reader), click)
            delivered.set()
            sock.sendall(ack + refreshed)
            done.wait(6)
            sock.shutdown(socket.SHUT_WR)
            self.assertEqual(reader.read(1), b'')  # No duplicated action after both exec calls.

        finished = self.upstream(serve)
        try:
            start = {'port': self.port, 'pair': self.token}
            with ThreadPoolExecutor(2) as pool:
                starts = list(pool.map(lambda _: self.rpc('start', **start), range(2)))
            self.assertEqual(starts, [{'ok': True}] * 2)
            wrong = self.rpc('start', port=self.port, pair=secrets.token_urlsafe(32))
            self.assertEqual(wrong, {'ok': False, 'error': 'session-conflict'})
            self.assertFalse(self.rpc('poll', after=1)['ok'])
            first = self.rpc('poll', after=0)
            self.assertEqual(first, self.rpc('poll', after=0))
            path = self.root / 'msg-1.bin'
            self.assertEqual(first, {'ok': True, 'kind': 'message', 'id': 1, 'size': len(large),
                                     'sha256': hashlib.sha256(large).hexdigest(), 'file': True})
            self.assertEqual(path.read_bytes(), large)
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(self.root.stat().st_mode), 0o700)
            self.assertNotIn(self.token.encode(), (self.root / 'owner').read_bytes())
            self.assertEqual(set(p.name for p in self.root.iterdir()), {'owner', 'control.sock', 'msg-1.bin'})
            cmd = {'id': 1, 'data': base64.b64encode(encode_message(click)).decode()}
            # Simulate a provider call that reached the broker but lost its
            # response. The following two retries must acknowledge the same send.
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as interrupted:
                interrupted.connect(str(self.root / relay.SOCKET))
                interrupted.sendall(json.dumps({'op': 'click', 'session': self.session, **cmd}).encode() + b'\n')
            self.assertTrue(delivered.wait(3))
            with ThreadPoolExecutor(2) as pool:
                results = list(pool.map(lambda _: self.rpc('click', **cmd), range(2)))
            self.assertEqual(results, [{'ok': True, 'id': 1}] * 2)
            self.assertFalse(self.rpc('click', id=1, data=base64.b64encode(encode_message(Click(1, 12, 13))).decode())['ok'])
            self.assertFalse(self.rpc('click', id=3, data=cmd['data'])['ok'])
            second = self.rpc('poll', after=1)
            self.assertEqual(base64.b64decode(second['data'], validate=True), ack)
            self.assertEqual(second, self.rpc('poll', after=1))
            self.assertFalse(path.exists())
            third = self.rpc('poll', after=2)
            self.assertEqual(base64.b64decode(third['data'], validate=True), refreshed)
            self.assertEqual(third['id'], 3)
            self.assertFalse(self.rpc('poll', after=0)['ok'])
            done.set()
            self.assertEqual(self.rpc('poll', after=3), {'ok': True, 'kind': 'closed'})
            self.assertEqual(self.rpc('close'), {'ok': True})
            self.assertFalse(self.root.exists())
            self.assertEqual(self.rpc('close'), {'ok': True})
            finished.result(8)
        finally:
            done.set()

    def test_rejected_pair_and_truncated_frame_never_expose_partial_file(self):
        def fail(sock, _):
            large = frame(1, 70000)
            sock.sendall(large[:800])

        closed = self.upstream(fail)
        self.assertEqual(self.rpc('start', port=self.port, pair=self.token), {'ok': False, 'error': 'upstream-unavailable'})
        closed.result(1)
        self.assertEqual(self.rpc('poll', after=0)['kind'], 'closed')
        self.assertFalse((self.root / 'msg-1.bin').exists())
        self.assertTrue(self.rpc('close')['ok'])
        self.assertFalse(self.root.exists())

    def test_request_is_not_an_open_file_or_socket_proxy(self):
        for req in [dict(op='start', port=9000, pair=self.token, host='example.com'),
                    dict(op='poll', after=0, path='/etc/passwd'), dict(op='click', id=True, data=''),
                    dict(op='shell', argv=['true']), dict(op='start', port=True, pair=self.token)]:
            with self.subTest(req=req):
                self.assertFalse(self.rpc(req.pop('op'), **req)['ok'])
        self.assertFalse(self.root.exists())

    def test_abandoned_viewer_releases_app_and_private_files_at_idle_timeout(self):
        def serve(sock, reader):
            sock.sendall(frame(1, 100000))
            self.assertEqual(reader.read(1), b'')

        worker = self.upstream(serve)
        # The product's timeout is fixed. Shorten it for this isolated process.
        source = SOURCE.partition("if __name__ == '__main__':")[0] + '\nIDLE = 0.5\nmain()\n'
        self.assertEqual(self.rpc('start', source=source, port=self.port, pair=self.token), {'ok': True})
        self.assertTrue((self.root / 'msg-1.bin').exists())
        worker.result(3)
        deadline = time.monotonic() + 2
        while self.root.exists() and time.monotonic() < deadline:
            time.sleep(.02)
        self.assertFalse(self.root.exists())

from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stderr
import http.client
import io
import os
import socket
import threading
import time
import unittest
from unittest.mock import patch

from cloud_render.wire import Click, ClickAck, Frame, Layer, read_message, write_message
from thin_client import viewer


def encoded(message):
    stream = io.BytesIO()
    write_message(stream, message)
    return stream.getvalue()


def frame(sequence):
    # Opaque transport payload. Real Skia decoding is covered in renderer tests.
    return Frame(sequence, 256, 128, (Layer(0, 0, 256, 128, b'skiapict' + bytes([sequence])),))


class ViewerTransport(unittest.TestCase):
    def setUp(self):
        local, self.cloud = socket.socketpair()
        self.cloud.settimeout(3)
        self.cloud.sendall(encoded(frame(1)))
        self.session = viewer.DisplaySession(local)
        self.server = viewer.ViewerServer(self.session)
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={'poll_interval': .01})
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.session.close()
        self.cloud.close()

    def request(self, path, method='GET', body=None, headers=None):
        conn = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=4)
        try:
            conn.request(method, path, body=body, headers=headers or {})
            response = conn.getresponse()
            return response.status, response.read()
        finally:
            conn.close()

    def auth(self):
        return {'Origin': self.server.origin, 'X-Viewer-Token': self.server.token,
                'Content-Type': 'application/octet-stream'}

    def test_same_origin_token_and_asset_allowlist(self):
        self.assertEqual(self.request('/')[0], 200)
        self.assertEqual(self.request('/?file=viewer.py')[0], 404)
        self.assertEqual(self.request('/frame')[0], 403)
        for key, value in [('Host', 'another.invalid'), ('Origin', 'http://localhost:22'),
                           ('Sec-Fetch-Site', 'cross-site'), ('X-Viewer-Token', 'other')]:
            with self.subTest(header=key):
                self.assertEqual(self.request('/frame', headers={**self.auth(), key: value})[0], 403)
        self.assertEqual(self.request('/frame', headers=self.auth()), (200, encoded(frame(1))))
        for path, mime in viewer.FILES.values():
            self.assertTrue((viewer.ASSETS / path).is_file(), path)

    def test_local_bounds_stale_confirmed_click_and_duplicate_submission(self):
        event = Click(1, 15.25, 64)
        for candidate, expected in [(Click(2, 0, 0), 409), (Click(1, 256, 5), 400)]:
            self.assertEqual(self.request('/click', 'POST', encoded(candidate), self.auth())[0], expected)
        self.assertEqual(self.request('/click', 'POST', encoded(event), {**self.auth(), 'Origin': ''})[0], 403)
        self.assertEqual(self.request('/click', 'POST', b'short', self.auth())[0], 400)
        with self.cloud.makefile('rb') as message, ThreadPoolExecutor(1) as pool:
            result = pool.submit(self.request, '/click', 'POST', encoded(event), self.auth())
            self.assertEqual(read_message(message, expected=Click), event)
            self.cloud.sendall(encoded(frame(2)))
            self.assertEqual(self.request('/frame?after=1', headers=self.auth()), (200, encoded(frame(2))))
            self.assertFalse(result.done())  # A new picture cannot acknowledge an input event.
            self.cloud.sendall(encoded(ClickAck(1, True)))
            self.assertEqual(result.result(), (204, b''))
        self.assertEqual(self.request('/click', 'POST', encoded(event), self.auth())[0], 409)
        self.assertEqual(self.request('/frame', headers=self.auth()), (200, encoded(frame(2))))

    def test_lost_ack_keeps_original_frame_and_never_replays(self):
        event = Click(1, 4, 2)
        with self.cloud.makefile('rb') as message, ThreadPoolExecutor(1) as pool:
            result = pool.submit(self.request, '/click', 'POST', encoded(event), self.auth())
            self.assertEqual(read_message(message, expected=Click), event)
            self.cloud.shutdown(socket.SHUT_WR)
            self.assertEqual(result.result()[0], 503)
            self.assertEqual(self.request('/click', 'POST', encoded(event), self.auth())[0], 503)
            self.assertIsNone(read_message(message))
        self.assertEqual(self.request('/frame?after=1', headers=self.auth())[0], 503)
        self.assertEqual(self.request('/frame', headers=self.auth()), (200, encoded(frame(1))))

    def test_cloud_rejects_click_after_local_validation(self):
        event = Click(1, 0, 0)
        with self.cloud.makefile('rb') as message, ThreadPoolExecutor(1) as pool:
            result = pool.submit(self.request, '/click', 'POST', encoded(event), self.auth())
            self.assertEqual(read_message(message, expected=Click), event)
            self.cloud.sendall(encoded(ClickAck(1, False)) + encoded(frame(2)))
            self.assertEqual(result.result()[0], 409)
            self.assertEqual(self.request('/frame?after=1', headers=self.auth()), (200, encoded(frame(2))))

    def test_refresh_and_last_tab_close_manage_the_browser_lifetime(self):
        tab1, tab2 = 'a' * 32, 'b' * 32
        headers = self.auth()
        with patch('thin_client.viewer.time.monotonic', return_value=1000):
            for tab in [tab1, tab2]:
                self.assertEqual(self.request('/frame', headers={**headers, 'X-Viewer-Tab': tab})[0], 200)
            self.assertEqual(self.request('/leave', 'POST', b'', {**headers, 'X-Viewer-Tab': tab1})[0], 204)
            self.assertEqual(self.server.exit_after, 1900)
            self.assertEqual(self.request('/leave', 'POST', b'', {**headers, 'X-Viewer-Tab': tab2})[0], 204)
            self.assertEqual(self.server.exit_after, 1010)
            self.assertEqual(self.request('/frame', headers={**headers, 'X-Viewer-Tab': tab1})[0], 200)
            self.assertEqual(self.server.exit_after, 1900)

    def test_automatic_open_uses_app_window(self):
        with patch('thin_client.app_window.open_app_window', return_value=True) as window:
            self.assertTrue(viewer.open_local_viewer(self.server.url))
            self.assertEqual(window.call_args.args[0], self.server.url)

    def test_blocking_system_browser_can_contact_viewer_during_launch(self):
        with viewer.ViewerServer(self.session) as server:
            observed = []
            def foreground_browser(url, on_closed):
                conn = http.client.HTTPConnection('127.0.0.1', server.server_port, timeout=3)
                try:
                    conn.request('GET', '/frame', headers={'X-Viewer-Token': server.token, 'X-Viewer-Tab': 'a' * 32})
                    response = conn.getresponse()
                    observed.append((url, response.read()))
                    on_closed()  # Closing the native target can bypass pagehide entirely.
                finally:
                    conn.close()
                return True
            with patch('thin_client.viewer.open_local_viewer', side_effect=foreground_browser):
                self.assertTrue(server.serve_browser())
            self.assertEqual(observed, [(server.url, encoded(frame(1)))])

    def test_pairing_precedes_first_frame_and_failed_pairing_never_opens_browser(self):
        pair = 'Q' * 43
        with socket.socket() as listener, ThreadPoolExecutor(1) as pool:
            listener.bind(('127.0.0.1', 0))
            listener.listen(1)
            def reject():
                with listener.accept()[0] as peer, peer.makefile('rb') as stream:
                    return stream.readline(100)
            seen = pool.submit(reject)
            diagnostic = io.StringIO()
            with redirect_stderr(diagnostic), patch.dict(os.environ, CLOUDWORKSPACE_RENDER_TOKEN=pair), \
                    patch('thin_client.viewer.open_local_viewer') as browser:
                self.assertEqual(viewer.main(['--port', str(listener.getsockname()[1])]), 1)
                browser.assert_not_called()
            self.assertEqual(seen.result(), b'CLRP-AUTH/1 ' + pair.encode('ascii') + b'\n')
            self.assertNotIn(pair, diagnostic.getvalue())


if __name__ == '__main__':
    unittest.main()

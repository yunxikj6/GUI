"""Default entry points for cloud desktop and local development."""
import http.client
import io
import os
import re
import sys
import tempfile
import threading
import unittest
from contextlib import contextmanager, redirect_stdout
from html.parser import HTMLParser
from pathlib import Path
from unittest.mock import Mock, patch
from urllib.parse import urljoin, urlsplit

import launcher
from test_browser import BROWSER, expect, sync_playwright
from test_desktop_clipboard_ui import ViewerHandler


class PageLinks(HTMLParser):
    def __init__(self, html):
        super().__init__()
        self.links = []
        self.assets = []
        self.feed(html.decode('utf-8'))

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == 'a' and 'href' in a:
            self.links.append(a['href'])
        if tag == 'script' and 'src' in a:
            self.assets.append(a['src'])
        if tag == 'link' and a.get('rel') == 'stylesheet':
            self.assets.append(a['href'])


@contextmanager
def serving(workspace, handler=launcher.Handler):
    with launcher.LocalServer(('127.0.0.1', 0), handler) as server:
        server.workspace = workspace
        if handler is ViewerHandler:
            server.peers, server.extended = [], False
        thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': .02}, daemon=True)
        thread.start()
        try:
            yield server
        finally:
            server.shutdown()
            thread.join(3)


def get(server, path):
    conn = http.client.HTTPConnection('127.0.0.1', server.server_port, timeout=3)
    try:
        conn.request('GET', path)
        reply = conn.getresponse()
        return reply.status, reply.headers, reply.read()
    finally:
        conn.close()


class DesktopEntry(unittest.TestCase):
    def assert_workbench(self, server, path):
        status, headers, body = get(server, path)
        self.assertEqual(status, 200)
        self.assertEqual(headers.get_content_type(), 'text/html')
        page = PageLinks(body)
        self.assertIn('/desktop/', page.links)
        assets = {Path(urlsplit(asset).path).name: urljoin(path, asset) for asset in page.assets}
        for name, mime in [('app.js', 'text/javascript'), ('proposals.js', 'text/javascript'), ('style.css', 'text/css')]:
            with self.subTest(name=name):
                status, headers, body = get(server, assets[name])
                self.assertEqual(status, 200)
                self.assertEqual(headers.get_content_type(), mime)
                self.assertEqual(body, (launcher.APP / 'web' / name).read_bytes())

    def test_cloud_root_and_return_to_workbench(self):
        remote = Mock(spec=launcher.InstaBox)
        with serving(remote) as server:
            status, headers, _ = get(server, '/')
            self.assertIn(status, (302, 307))
            self.assertEqual(headers['Location'], '/desktop/')
            status, headers, html = get(server, headers['Location'])
            self.assertEqual(status, 200)
            self.assertEqual(headers.get_content_type(), 'text/html')
            page = PageLinks(html)
            self.assertIn('/workbench/', page.links)
            self.assertIn('/desktop/viewer.js', [urljoin('/desktop/', path) for path in page.assets])
            self.assert_workbench(server, '/workbench/')
            remote.api.assert_not_called()

    def test_local_root_keeps_its_workbench(self):
        with tempfile.TemporaryDirectory() as tmp:
            with serving(launcher.LocalWorkspace(Path(tmp))) as server:
                self.assert_workbench(server, '/')

    def test_printed_entry_and_system_browser_match_each_launch_mode(self):
        with tempfile.TemporaryDirectory() as tmp:
            for options, path in [([], '/desktop/'), (['--workbench'], '/workbench/'),
                                  (['--local', '--project', tmp], '/')]:
                for headless in (True, False):
                    with self.subTest(options=options, headless=headless):
                        browser = Mock()
                        # Run the scheduled launch synchronously, keeping the real browser closed.
                        def immediate_timer(delay, callback, args=()):
                            return Mock(start=lambda: callback(*args))
                        args = ['launcher.py', '--port', '0', *options]
                        if headless:
                            args.append('--no-browser')
                        stdout = io.StringIO()
                        with patch.object(sys, 'argv', args), patch.dict(os.environ, {'INSTABOX_API_KEY': 'fixture'}), \
                             patch.object(launcher, 'InstaBox', autospec=True) as box, \
                             patch.object(launcher.LocalServer, 'serve_forever'), \
                             patch.object(launcher.threading, 'Timer', side_effect=immediate_timer), \
                             patch.object(launcher.webbrowser, 'open', browser), redirect_stdout(stdout):
                            self.assertEqual(launcher.main(), 0)
                        (url,) = re.findall(r'http://127\.0\.0\.1:\d+/\S*', stdout.getvalue())
                        self.assertEqual(urlsplit(url).path, path)
                        if headless:
                            browser.assert_not_called()
                        else:
                            browser.assert_called_once_with(url)
                        box.return_value.api.assert_not_called()
                        box.return_value.setup.assert_not_called()

    @unittest.skipUnless(sync_playwright and BROWSER, 'browser acceptance requires Playwright and Chromium')
    def test_remote_default_opens_viewing_session_without_taking_input(self):
        remote = Mock(spec=launcher.InstaBox)
        remote.machine_id, remote.project = 'm_entryfixture', '/data/example'
        remote.rpc.return_value = {'ok': False, 'error': {'code': 'offline', 'message': 'offline'}}
        remote.api.return_value = {'running': True, 'installed': True, 'agentPausable': True,
                                   'kind': 'x11', 'controller': 'agent', 'yours': False}
        with serving(remote, ViewerHandler) as server, sync_playwright() as driver:
            browser = driver.chromium.launch(executable_path=BROWSER, args=['--no-sandbox'])
            try:
                page = browser.new_page()
                origin = f'http://127.0.0.1:{server.server_port}'
                page.goto(origin + '/')
                expect(page).to_have_url(origin + '/desktop/')
                expect(page.locator('#status')).to_have_text('正在观看')
                expect(page.get_by_role('button', name='接管输入')).to_be_enabled()
                self.assertEqual([call.args[2]['action'] for call in remote.api.call_args_list], ['status'])
                self.assertTrue(server.peers)
            finally:
                browser.close()


if __name__ == '__main__':
    unittest.main()

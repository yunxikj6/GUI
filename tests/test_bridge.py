"""Loopback browser boundary and API error reporting."""
import http.client
import json
import os
import threading
import unittest
from unittest.mock import patch

import launcher
from instabox import BridgeError, InstaBox


class TestWorkerEnvironment(unittest.TestCase):
    def test_remote_git_path_comes_from_launcher_and_survives_resume(self):
        with patch.object(InstaBox, 'api', return_value={'machines': [{'id': 'm_test'}]}):
            box = InstaBox('fixture', project='/data/project')
        box.worker = '/data/runtime/workspace.py'
        payload = {'op': 'status', 'CLOUD_WORKSPACE_GIT': '/ignored/browser/value'}
        choice = '/opt/git current/bin/git'
        with patch.dict(os.environ, {'CLOUD_WORKSPACE_GIT': choice}), patch.object(box, 'ensure_running'), \
             patch.object(box, 'exec', side_effect=[BridgeError('machine_suspended', 'paused'), '{"ok":true}']) as run:
            self.assertTrue(box.rpc(payload)['ok'])
        self.assertEqual(len(run.call_args_list), 2)
        for call in run.call_args_list:
            self.assertEqual(call.kwargs['envs'], {'CLOUD_WORKSPACE_ROOT': box.project, 'CLOUD_WORKSPACE_GIT': choice})
        with patch.dict(os.environ, {}, clear=True), patch.object(box, 'exec', return_value='{"ok":true}') as run:
            self.assertTrue(box.rpc(payload)['ok'])
            self.assertEqual(run.call_args.kwargs['envs'], {'CLOUD_WORKSPACE_ROOT': box.project})


class TestHttpBoundary(unittest.TestCase):
    def setUp(self):
        self.server = launcher.LocalServer(('127.0.0.1', 0), launcher.Handler)
        class Fake:
            machine_id = 'm_example'
            project = '/data/one'
            def rpc(self, request):
                return {'ok': True, 'data': {'project': 'one'}}
        self.server.workspace = Fake()
        thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.port = self.server.server_port

    def post(self, req, **headers):
        conn = http.client.HTTPConnection('127.0.0.1', self.port, timeout=3)
        try:
            conn.request('POST', '/api/rpc', json.dumps(req), {'Content-Type':'application/json', **headers})
            res = conn.getresponse()
            return res.status, json.loads(res.read())
        finally:
            conn.close()

    def test_only_expected_origin_and_host(self):
        for header in ({'Origin':'https://other.invalid'}, {'Host':'other.invalid'}, {'Sec-Fetch-Site':'cross-site'}):
            self.assertEqual(self.post({'op':'status'}, **header)[0], 403)
        self.assertEqual(self.post({'op':'status'}, **{'Content-Type':'text/plain'})[0], 415)
        valid, expected = self.post({'op':'status'}, Origin=f'http://127.0.0.1:{self.port}')
        self.assertEqual(valid, 200)
        self.assertEqual(expected['data']['mode'], 'instabox')
        self.assertIn('workspaceId', expected['data'])

    def test_worker_bootstrap_not_available_from_browser(self):
        self.assertEqual(self.post({'op':'init'})[0], 400)

    def test_launcher_identifies_project_without_contacting_disconnected_machine(self):
        _, online = self.post({'op': 'status'})
        conn = http.client.HTTPConnection('127.0.0.1', self.port, timeout=3)
        self.addCleanup(conn.close)
        with patch.object(self.server.workspace, 'rpc', side_effect=AssertionError('unexpected cloud call')):
            conn.request('GET', '/api/workspace')
            response = conn.getresponse()
            self.assertEqual(response.status, 200)
            self.assertEqual(json.loads(response.read()), online)
            self.server.workspace.machine_id = 'm_elsewhere'
            conn.request('GET', '/api/workspace')
            other = json.loads(conn.getresponse().read())['data']
            self.assertNotEqual(online['data']['workspaceId'], other['workspaceId'])
            conn.request('GET', '/api/workspace', headers={'Origin': 'https://other.invalid'})
            self.assertEqual(conn.getresponse().status, 403)

    def test_stale_workspace_requests_never_reach_new_project(self):
        _, status = self.post({'op': 'status'})
        expected = {'X-Workspace-Id': status['data']['workspaceId']}
        self.assertEqual(self.post({'op': 'tasks'}, **expected)[0], 200)
        self.server.workspace.project = '/data/moved/one'
        with patch.object(self.server.workspace, 'rpc', side_effect=AssertionError('unexpected cloud call')):
            for operation in ('save', 'start', 'cancel', 'events', 'file'):
                with self.subTest(operation=operation):
                    code, body = self.post({'op': operation}, **expected)
                    self.assertEqual(code, 409)
                    self.assertEqual(body['error']['code'], 'workspace_changed')


if __name__ == '__main__':
    unittest.main()

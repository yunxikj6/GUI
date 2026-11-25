"""The desktop can start without tools; only workspace requests initialize them."""
import concurrent.futures
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

from instabox import BridgeError, InstaBox


class LazyWorkspaceTests(unittest.TestCase):
    def box(self, **kwargs):
        with patch.object(InstaBox, 'api', return_value={'machines': [{'id': 'm_fixture'}]}):
            return InstaBox('fixture', **kwargs)

    def test_waking_for_desktop_never_reads_workspace_source_or_executes_a_worker(self):
        with tempfile.TemporaryDirectory() as directory:
            box = self.box(source=Path(directory) / 'missing')
            with patch.object(box, 'api', return_value={'state': 'running'}) as api, patch.object(box, 'exec') as run:
                box.ensure_running()
                api.assert_called_once_with('GET', '/v1/machines/m_fixture')
                self.assertIsNone(box.worker)
                with self.assertRaises(BridgeError) as caught:
                    box.rpc({'op': 'status'})
                self.assertEqual(caught.exception.code, 'worker_source')
                self.assertIsNone(box.worker)
                run.assert_not_called()

    def test_concurrent_requests_share_one_install_and_init_then_execute_in_parallel(self):
        box = self.box()
        booting, release = threading.Event(), threading.Event()
        readers = threading.Barrier(4)
        operations = []

        def execute(argv, stdin='', envs=None):
            if argv[1] == '-c':
                operations.append('install')
                return 'ready'
            request = json.loads(stdin)
            operations.append(request['op'])
            if request['op'] == 'init':
                booting.set()
                self.assertTrue(release.wait(5))
                return '{"ok":true}'
            readers.wait(5)
            return json.dumps({'ok': True, 'data': request['path']})

        with patch.object(box, 'ensure_running'), patch.object(box, 'exec', side_effect=execute):
            with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
                jobs = [pool.submit(box.rpc, {'op': 'file', 'path': str(i)}) for i in range(4)]
                try:
                    self.assertTrue(booting.wait(5))
                    self.assertIsNone(box.worker)
                    self.assertEqual(operations, ['install', 'init'])
                finally:
                    release.set()
                self.assertEqual([f.result(6)['data'] for f in jobs], ['0', '1', '2', '3'])
        self.assertEqual(operations, ['install', 'init'] + ['file'] * 4)
        self.assertTrue(box.worker.endswith('/workspace.py'))

    def test_failed_preparation_retries_and_custom_status_runs_once_per_attempt(self):
        box = self.box(project='/data/custom')
        ready = '{"ok":true,"data":{"project":"custom"}}'
        attempts = [BridgeError('network', 'offline'), 'ready', '{"ok":false,"error":{"code":"not_found"}}',
                    'ready', ready]
        with patch.object(box, 'ensure_running'), patch.object(box, 'exec', side_effect=attempts) as run:
            for code in ('network', 'not_found'):
                with self.assertRaises(BridgeError) as caught:
                    box.rpc({'op': 'status'})
                self.assertEqual(caught.exception.code, code)
                self.assertIsNone(box.worker)
            self.assertEqual(box.rpc({'op': 'status'}), json.loads(ready))
        self.assertEqual([json.loads(c.args[1]) for c in run.call_args_list if c.args[0][1] != '-c'],
                         [{'op': 'status'}, {'op': 'status'}])

    def test_explicit_setup_and_existing_worker_survive_a_failed_replacement(self):
        box = self.box()
        source = Path(__file__).resolve().parents[1] / 'cloud'
        run = Mock(side_effect=['ready', '{"ok":true}', 'ready', '{"ok":false}', '{"ok":true}'])
        with patch.object(box, 'ensure_running'), patch.object(box, 'exec', run):
            box.setup(source)
            original = box.worker
            self.assertIsNotNone(original)
            with tempfile.TemporaryDirectory() as directory:
                modified = Path(directory)
                for file in source.glob('*.py'):
                    (modified / file.name).write_bytes(file.read_bytes() + b'\n')
                with self.assertRaises(BridgeError):
                    box.setup(modified)
            self.assertEqual(box.worker, original)
            self.assertTrue(box.rpc({'op': 'tree'})['ok'])
        self.assertEqual(run.call_args.args[0], ['python3', original, '--stdin'])

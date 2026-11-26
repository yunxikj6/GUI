import json
import os
import threading
import unittest
from unittest.mock import patch

from thin_client import app_window


class PrivateAppWindow(unittest.TestCase):
    def test_spawn_uses_public_origin_and_discards_cloud_credentials(self):
        origin = 'http://127.0.0.1:41234/'
        token = 'A' * 43
        read_fd, write_fd = os.pipe()
        try:
            with patch.dict(os.environ, INSTABOX_API_KEY='test-private', CLOUDWORKSPACE_RENDER_TOKEN=token), \
                    patch.object(app_window.subprocess, 'Popen') as spawn:
                app_window._spawn('/installed/browser', origin, '/private-profile', read_fd, write_fd)
            args = spawn.call_args.args[0]
            self.assertIn('--app=' + origin, args)
            self.assertIn('--remote-debugging-pipe', args)
            self.assertIn('--user-data-dir=/private-profile', args)
            self.assertFalse(any('--remote-debugging-port' in arg or '--no-sandbox' in arg or token in arg for arg in args))
            env = spawn.call_args.kwargs['env']
            self.assertNotIn('INSTABOX_API_KEY', env)
            self.assertNotIn('CLOUDWORKSPACE_RENDER_TOKEN', env)
        finally:
            os.close(read_fd)
            os.close(write_fd)

    def test_deliver_to_our_target_once_per_document_and_resume_after_reload(self):
        origin = 'http://127.0.0.1:41234/'
        token = 'B' * 43
        read_cmd, write_cmd = os.pipe()
        read_reply, write_reply = os.pipe()
        ready = threading.Event()
        delivered = []
        expressions = []
        errors = []

        def instance():
            try:
                with os.fdopen(read_cmd, 'rb') as src, os.fdopen(write_reply, 'wb') as dst:
                    buf = bytearray()
                    while True:
                        byte = src.read(1)
                        if not byte:
                            return
                        if byte != b'\0':
                            buf.extend(byte)
                            continue
                        request = json.loads(buf)
                        buf.clear()
                        method, params = request['method'], request['params']
                        result = {}
                        if method == 'Target.getTargets':
                            result = {'targetInfos': [{'type': 'page', 'url': 'https://another.invalid/', 'targetId': 'other'},
                                                      {'type': 'page', 'url': origin, 'targetId': 'app'}]}
                        elif method == 'Target.attachToTarget':
                            self.assertEqual(params['targetId'], 'app')
                            result = {'sessionId': 'app-session'}
                        elif method == 'Runtime.evaluate':
                            expressions.append(params['expression'])
                            result = {'result': {'type': 'function', 'objectId': f'document-{len(delivered)}'}}
                        elif method == 'Runtime.callFunctionOn':
                            self.assertEqual(params['objectId'], f'document-{len(delivered)}')
                            delivered.append(params['arguments'][0]['value'])
                            result = {'result': {'value': True}}
                        else:
                            self.assertIn(method, ('Page.enable', 'Runtime.releaseObject'))
                        dst.write(json.dumps({'id': request['id'], 'result': result}).encode() + b'\0')
                        if method == 'Runtime.releaseObject':
                            if len(delivered) == 2:
                                return
                            dst.write(b'{"method":"Page.loadEventFired","sessionId":"app-session"}\0')
                        dst.flush()
            except BaseException as exc:
                errors.append(exc)

        worker = threading.Thread(target=instance, daemon=True)
        worker.start()
        with os.fdopen(read_reply, 'rb') as src, os.fdopen(write_cmd, 'wb') as dst, self.assertRaises(OSError):
            app_window._drive(src, dst, origin, token, ready.set)
        worker.join(3)
        self.assertFalse(worker.is_alive())
        if errors:
            raise errors[0]
        self.assertTrue(ready.is_set())
        self.assertEqual(delivered, [token, token])
        self.assertTrue(all(origin in expression and token not in expression for expression in expressions))

    def test_external_target_and_invalid_fragment_never_launch(self):
        with patch.object(app_window, '_installed_browser', side_effect=AssertionError):
            for url in ('https://example.test/#' + 'A' * 43, 'http://127.0.0.1:50/#short',
                        'http://127.0.0.1:50/other#' + 'B' * 43):
                self.assertFalse(app_window.open_app_window(url))

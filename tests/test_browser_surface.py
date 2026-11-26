import base64
from io import BytesIO
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import AsyncMock, Mock

from cloud_render.browser_surface import OFFLINE_TARGET, SystemBrowserWindow, _identify_idle_layers, _window_identity
from cloud_render.chromium_capture import CaptureError


def layer(layer_id, ref):
    return {'layer_id': layer_id, 'draws_content': True, 'opacity': 1, 'bounds': {'width': 60, 'height': 40},
            'pictures': [{'id_ref': ref}]}


def record(name):
    return {'skp64': name, 'params': {'layer_rect': [0, 0, 60, 40]}}


class PrivateLayerComparison(unittest.IsolatedAsyncioTestCase):
    class LayerSession:
        def __init__(self, source_images):
            self.images = source_images
            self.released = []

        async def send(self, method, args):
            if method == 'LayerTree.makeSnapshot':
                return {'snapshotId': 'current-' + args['layerId']}
            if method == 'LayerTree.loadSnapshot':
                return {'snapshotId': args['tiles'][0]['picture']}
            if method == 'LayerTree.replaySnapshot':
                from PIL import Image
                name = self.images[args['snapshotId']]
                data = BytesIO()
                shade = sum(name.encode()) % 256
                Image.new('RGBA', (60, 40), (shade, 0, 0, 255)).save(data, format='PNG')
                encoded = 'data:image/png;base64,' + base64.b64encode(data.getvalue()).decode('ascii')
                return {'dataURL': encoded}
            if method == 'LayerTree.snapshotCommandLog':
                return {'commandLog': []}
            if method == 'LayerTree.releaseSnapshot':
                self.released.append(args['snapshotId'])
                return {}
            raise AssertionError(method)

    async def test_same_geometry_is_resolved_by_actual_layer_identity_and_full_original_rendering(self):
        tree = {'active_tree': {'layers': [layer(4, 'a'), layer(6, 'b')]}}
        background, button = record('native-background'), record('native-button')
        target = self.LayerSession({'current-4': 'first', 'current-6': 'second',
                                    'native-background': 'first', 'native-button': 'second'})
        pictures = {}
        await _identify_idle_layers(target, tree, pictures, [button, background])
        self.assertIs(pictures[4, 'a'], background)
        self.assertIs(pictures[6, 'b'], button)
        self.assertCountEqual(target.released, target.images)

    async def test_indistinguishable_candidates_do_not_grant_a_layer(self):
        tree = {'active_tree': {'layers': [layer(1, 'r')]}}
        for second in ('also-current', 'unrelated'):
            with self.subTest(second=second):
                images = {'current-1': 'also-current', 'left': second, 'right': second}
                target = self.LayerSession(images)
                pictures = {}
                with self.assertRaises(CaptureError):
                    await _identify_idle_layers(target, tree, pictures, [record('left'), record('right')])
                self.assertEqual(pictures, {})
                self.assertCountEqual(target.released, images)

    async def test_failing_comparison_releases_snapshot(self):
        target = self.LayerSession({})
        with self.assertRaises(KeyError):
            await _identify_idle_layers(target, {'active_tree': {'layers': [layer(1, 'r')]}}, {}, [record('left')])
        self.assertEqual(target.released, ['current-1'])


@unittest.skipUnless(sys.platform.startswith('linux'), 'dedicated Linux browser process')
class OfflineWindowIdentity(unittest.IsolatedAsyncioTestCase):
    async def test_binding_requires_the_fixed_query_free_offline_document_and_known_browser_pid(self):
        pid = os.getpid()
        processes = {'processInfo': [{'type': 'browser', 'id': pid}]}
        page = Mock()
        page.context.pages = [page]
        page.context.browser.contexts = [page.context]
        page.is_closed.return_value = False
        frame = {'id': 'F', 'loaderId': 'L', 'url': 'chrome-error://chromewebdata/', 'unreachableUrl': OFFLINE_TARGET}
        info = {'type': 'page', 'url': OFFLINE_TARGET, 'targetId': 'F'}
        info_response = {'targetInfo': info}
        frame_response = {'frameTree': {'frame': frame}}
        metrics = {'cssVisualViewport': {'offsetX': 0, 'offsetY': 0, 'clientWidth': 900, 'clientHeight': 633}}
        def browser_reply(method, args=None):
            return processes if method == 'SystemInfo.getProcessInfo' else {'windowId': 42, 'bounds': {'windowState': 'normal'}}
        def target_reply(method, args=None):
            return {'Target.getTargetInfo': info_response, 'Page.getFrameTree': frame_response,
                    'Page.getLayoutMetrics': metrics, 'Runtime.evaluate': {'result': {'value': [900, 633, 1]}}}[method]
        browser, target = Mock(), Mock()
        browser.send, target.send = AsyncMock(side_effect=browser_reply), AsyncMock(side_effect=target_reply)
        async def identity():
            return await _window_identity(page, browser, target, pid, Path(sys.executable))
        expected = SystemBrowserWindow(pid, 42, 'F', 'L')
        self.assertEqual(await identity(), (expected, (900, 633)))
        self.assertEqual(expected.frame_id, 'F')
        for field, changed in [('unreachableUrl', OFFLINE_TARGET + '?fixture'), ('url', OFFLINE_TARGET), ('id', 'new')]:
            with self.subTest(field=field):
                old = frame[field]
                frame[field] = changed
                with self.assertRaises(CaptureError):
                    await identity()
                frame[field] = old
        processes['processInfo'][0]['id'] += 1
        with self.assertRaises(CaptureError):
            await identity()

import gzip
import hashlib
import http.server
import json
import os
from pathlib import Path
import shutil
import threading
import unittest

try:
    from playwright.sync_api import Error as PlaywrightError, sync_playwright
except ImportError:
    sync_playwright = None


CLIENT = Path(__file__).resolve().parents[1] / 'thin_client'
BROWSER = os.environ.get('CLOUD_WORKSPACE_TEST_CHROMIUM') or shutil.which('chromium') or shutil.which('google-chrome')
# SkPictureRecorder v110. Two adjacent red and blue rectangles recorded with
# nonzero cull bounds [10, 20, 18, 28]. No image or font resources are needed.
RECTS = ('c2tpYXBpY3RuAAAAAAAgQQAAoEEAAJBBAADgQQFkYWVyMAAAABgAABUBAAAAAAAgQQAAoEEAAGBBAADgQRgAABUC'
         'AAAAAABgQQAAoEEAAJBBAADgQXRjYWYEAAAAAAAAAGNmcHQAAAAAeWFyYUgAAAAgdG5wAgAAAAAAAAAAAIBAAACAPwAA'
         'AAAAAAAAAACAPwADAAAAAAAAAACAQAAAAAAAAAAAAACAPwAAgD8AAwAAZ3VscwAAAAAgZm9l')
# Synthetic SkPicture of an 8x8 rectangle with a SrcIn color filter at alpha
# 30. The base CanvasKit build silently paints this record as fully opaque.
FILTER = ('c2tpYXBpY3RuAAAAAAAAAAAAAAAAAABBAAAAQQFkYWVyGAAAABgAABUBAAAAAAAAAAAAAAAAAABBAAAAQXRjYWYW'
          'AAAAAQAAABFTa01vZGVDb2xvckZpbHRlcmNmcHQAAAAAeWFyYVwAAAAgdG5wAQAAAAAAAAAAAIBA0dDQPeHg'
          '4D35+Pg9AACAPwADAAIAAAAAAAAAAAAAAAABAAAAFAAAANHQ0D3h4OA9+fj4PfHw8D0FAAAAAAAAAAAAAABndW'
          'xzAAAAACBmb2U=')


class Assets(unittest.TestCase):
    def test_shipped_runtime_matches_pinned_upstream_bytes(self):
        directory = CLIENT / 'vendor'
        pins = json.loads((directory / 'CanvasKit.json').read_text())['upstreamFilesSha256']
        loader = (directory / 'canvaskit.js').read_bytes()
        wasm = gzip.decompress((directory / 'canvaskit.wasm.gz').read_bytes())
        self.assertEqual(hashlib.sha256(loader).hexdigest(), pins['package/bin/full/canvaskit.js'])
        self.assertEqual(hashlib.sha256(wasm).hexdigest(), pins['package/bin/full/canvaskit.wasm'])


@unittest.skipUnless(sync_playwright and BROWSER, 'requires Chromium and Playwright')
class Replay(unittest.TestCase):
    def test_local_layer_replay_letterboxing_and_resize_offline(self):
        class Handler(http.server.SimpleHTTPRequestHandler):
            def __init__(self, *args):
                super().__init__(*args, directory=CLIENT)

            def log_message(self, *_):
                pass

        server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.addCleanup(server.server_close)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.shutdown)
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(executable_path=BROWSER, headless=True, chromium_sandbox=True)
            context = browser.new_context(viewport={'width': 320, 'height': 240})
            page = context.new_page()
            page.goto(f'http://127.0.0.1:{server.server_port}/vendor/CanvasKit.json')
            page.set_content('<style>*{margin:0}canvas{display:block;width:100vw;height:100vh}</style><canvas></canvas>')
            page.evaluate('''async value => {
              const {createPictureRenderer} = await import('/renderer.js');
              const canvas = document.querySelector('canvas');
              window.renderer = await createPictureRenderer(canvas);
              const skp = Uint8Array.from(atob(value), c => c.charCodeAt(0));
              renderer.present({width: 32, height: 24, layers: [
                {x: 0, y: 0, width: 32, height: 24, skp}, {x: 14, y: 8, width: 8, height: 8, skp}
              ]});
              window.pixel = (x, y) => [...canvas.getContext('2d').getImageData(x, y, 1, 1).data];
            }''', RECTS)
            self.assertEqual(page.evaluate('pixel(165, 95)'), [255, 0, 0, 255])
            self.assertEqual(page.evaluate('pixel(165, 25)'), [0, 0, 255, 255])
            with self.assertRaisesRegex(PlaywrightError, '云端绘图'):
                page.evaluate('renderer.present({width: 32, height: 24, layers: [{skp: new Uint8Array(3)}]})')
            requests = []
            page.on('request', lambda request: requests.append(request.url))
            context.set_offline(True)
            page.set_viewport_size({'width': 640, 'height': 240})
            page.wait_for_function("document.querySelector('canvas').width === 640")
            self.assertEqual(page.evaluate('pixel(325, 95)'), [255, 0, 0, 255])
            self.assertEqual(page.evaluate('pixel(10, 95)'), [0, 0, 0, 0])
            self.assertEqual(page.evaluate('renderer.position(325, 95)'), {'x': 16.5, 'y': 9.5})
            self.assertIsNone(page.evaluate('renderer.position(10, 95)'))
            self.assertEqual(requests, [])
            page.evaluate('''value => renderer.present({width: 8, height: 8, layers: [{
              x: 0, y: 0, width: 8, height: 8, skp: Uint8Array.from(atob(value), c => c.charCodeAt(0))
            }]})''', FILTER)
            self.assertEqual(page.evaluate('pixel(320, 120)[3]'), 30)
            page.evaluate('renderer.destroy()')
            browser.close()

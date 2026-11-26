import hashlib
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from PIL import Image, ImageDraw

from experiments.mutter_app.bind import read_witness
from experiments.mutter_app.capture import business_digest, window_origin
from experiments.mutter_app.controls import (
    ORIGINAL_SHA256,
    WitnessError,
    caption_layers,
)


class OriginalWindowSourceTest(unittest.TestCase):
    def test_source_reference_excludes_only_reserved_native_region(self):
        app = Image.new('RGB', (1090, 760), 'white')
        digest = business_digest(app)
        app.putpixel((1026, 35), (1, 2, 3))
        self.assertEqual(digest, business_digest(app))
        app.putpixel((1025, 35), (1, 2, 3))
        self.assertNotEqual(digest, business_digest(app))

    def test_source_reference_preserves_rgb_bytes_for_translucent_pixels(self):
        raw = bytes((15, 82, 164, 0, 12, 126, 180, 189)) * (1090 * 760 // 2)
        image = Image.frombytes('RGBA', (1090, 760), raw)
        rgb = image.convert('RGB')
        expected = rgb.crop((0, 0, 1026, 36)).tobytes() + rgb.crop((0, 36, 1090, 760)).tobytes()
        self.assertEqual(business_digest(image), hashlib.sha256(expected).hexdigest())
        self.assertEqual(business_digest(rgb), business_digest(image))
        r, g, b, a = image.split()
        self.assertEqual(image.tobytes('raw', 'BGRA'), Image.merge('RGBA', (b, g, r, a)).tobytes())

    def test_private_display_pointer_finds_real_origin(self):
        app = Image.new('RGB', (1090, 760), 'white')
        draw = ImageDraw.Draw(app)
        for i in range(128):
            draw.rectangle((490 + i, 300, 490 + i, 400), fill=(i, 2 * i, 255 - i))
        desktop = Image.new('RGB', (1280, 900), '#581b42')
        desktop.paste(app, (100, 69))
        self.assertEqual(window_origin(desktop, app), (100, 69))

    def test_refuse_browser_or_geometry_outside_known_state(self):
        with self.assertRaises(WitnessError):
            caption_layers([], browser_pid=1, geometry=(1090, 760, 2, 1026, 36, 'light'))
        with self.assertRaises(WitnessError):
            caption_layers([], browser_pid=1, geometry=(1090, 760, 1, 1026, 36, 'light'))

    def test_wire_offset_requires_same_actual_commit_and_gl_resource(self):
        authority = {
            'pid': 71,
            'startTicks': 449,
            'device': 8,
            'inode': 10,
            'binarySha256': ORIGINAL_SHA256,
            'target': 'app://-/index.html',
        }
        lines = [
            'XDG_SURFACE 1 71 40 33',
            'TOPLEVEL 1 40 41',
            'APP_ID 1 41 chatgpt',
            'PEER 1 71 449 8 10 33 12 7',
            'COMMIT 1 71 33 7',
            'SHM 7 4360 760 ' + hashlib.sha256(b'test').hexdigest(),
            'COGL_CREATE 7 0x222',
            'GL 7 23 3553',
            'WIRE 7 188 98 0 0 1090 760 3313600',
        ]
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / 'observer'
            path.write_text('\n'.join(lines) + '\n')
            role, marks = read_witness(path, authority)
            self.assertEqual(role['surface'], 33)
            self.assertEqual(marks[188][0], 7)
            for wrong in (
                dict(authority, pid=72),
                dict(authority, startTicks=448),
                dict(authority, inode=11),
                dict(authority, binarySha256='0' * 64),
            ):
                with self.assertRaises(WitnessError):
                    read_witness(path, wrong)
            path.write_text('\n'.join(line for line in lines if not line.startswith('COMMIT')))
            with self.assertRaises(WitnessError):
                read_witness(path, authority)


class LiveMutterSourceTest(unittest.TestCase):
    def test_unfinished_owned_scope_cannot_become_desktop(self):
        from experiments.mutter_app.live import Journal

        with TemporaryDirectory() as tmp:
            path = Path(tmp) / 'owner.log'
            path.write_text(
                'ACCESS 11 7 22 1 10\nSHM 1 40 9 abc\nCOGL_CREATE 1 0x10\nWIRE 1 64 5 0 0 10 9 360\n'
            )
            witness = Journal(path)
            try:
                self.assertIsNone(witness.at(64))
                with path.open('a') as out:
                    out.write('GL 1 12 3553\nEN')
                self.assertIsNone(witness.at(64))
                with path.open('a') as out:
                    out.write('D 1\nORIGIN 476 UNCLASSIFIED_SHM 8 0 0 10 9 360\n')
                kind, desc, metadata = witness.at(64)
                self.assertEqual((kind, desc), ('SHM', [5, 0, 0, 10, 9, 360]))
                self.assertEqual(metadata['gl'], (12, 3553))
                self.assertEqual(witness.at(476)[0], 'UNCLASSIFIED_SHM')
                self.assertIsNone(witness.at(64))
            finally:
                witness.file.close()

    def test_only_commits_within_the_pending_drawing_can_wait_for_approval(self):
        import json
        from unittest.mock import patch

        from experiments.mutter_app.live import Inspector, initialize

        with TemporaryDirectory() as tmp:
            out = Path(tmp)
            channel = out / 'pair'
            initialize(channel)
            inspector = Inspector(out, channel, interactive=True)
            (out / 'scenes.json').write_text('{}')
            (out / 'pending-scene.json').write_text(
                json.dumps({'businessRgbSha256': 'rgb', 'witnessByteStart': 51})
            )
            with patch('experiments.mutter_app.live.time.sleep') as sleep:
                self.assertEqual(inspector.manual_scene('rgb', 50), (None, None))
                sleep.assert_not_called()
                self.assertEqual(inspector.manual_scene('other', 52), (None, None))
                sleep.assert_not_called()

            raw = b'original recording'
            picture = hashlib.sha256(raw).hexdigest()
            (out / 'drawings').mkdir()
            (out / 'drawings' / (picture + '.clrp')).write_bytes(raw)

            def approve(_):
                (out / 'scenes.json').write_text(
                    json.dumps({'rgb': {'pictureSha256': picture, 'witnessByteStart': 51, 'bytes': len(raw)}})
                )

            with patch('experiments.mutter_app.live.time.sleep', side_effect=approve) as sleep:
                self.assertEqual(inspector.manual_scene('rgb', 51), ('approved', bytes.fromhex(picture)))
                sleep.assert_called_once()
                self.assertEqual(inspector.manual_scene('rgb', 50), (None, None))

    def test_only_a_new_physical_commit_rearms_a_sample_that_observed_a_different_surface(self):
        import json
        from unittest.mock import patch

        from experiments.mutter_app.capture import publish
        from experiments.mutter_app.live import Inspector, initialize

        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            pair = root / 'pair'
            initialize(pair)
            inspector = Inspector(root, pair, interactive=True)
            (root / 'scenes.json').write_text('{}')
            request = root / 'requested-scene.json'
            receipt = root / 'unmatched-scene.json'
            with patch('experiments.mutter_app.live.time.monotonic', return_value=1.0):
                inspector.manual_scene('caret', 100)
                first = json.loads(request.read_text())
                inspector.manual_scene('caret', 110)
                self.assertEqual(json.loads(request.read_text()), first)
                publish(receipt, {'businessRgbSha256': 'other', 'witnessByte': 100})
                inspector.manual_scene('caret', 120)
                self.assertEqual(json.loads(request.read_text()), first)
                publish(receipt, first)
                inspector.manual_scene('caret', 100)
                self.assertEqual(json.loads(request.read_text()), first)
                inspector.manual_scene('caret', 130)
                self.assertEqual(
                    json.loads(request.read_text()), {'businessRgbSha256': 'caret', 'witnessByte': 130}
                )
                inspector.manual_scene('caret', 140)
                self.assertEqual(json.loads(request.read_text())['witnessByte'], 130)

    def test_private_channels_use_new_session_and_reject_reuse(self):
        import json
        import stat

        from experiments.mutter_app.live import initialize

        with TemporaryDirectory() as tmp:
            channel = Path(tmp) / 'pair'
            initialize(channel)
            self.assertEqual(channel.stat().st_mode & 0o777, 0o700)
            for name in ('graphics.fifo', 'input.fifo'):
                self.assertTrue(stat.S_ISFIFO((channel / name).stat().st_mode))
                self.assertEqual((channel / name).stat().st_mode & 0o777, 0o600)
            self.assertEqual(
                len(bytes.fromhex(json.loads((channel / 'session.json').read_text())['session'])), 32
            )
            self.assertEqual((channel / 'session.json').stat().st_mode & 0o777, 0o600)
            with self.assertRaises(FileExistsError):
                initialize(channel)

    def test_user_moves_and_clicks_are_applied_in_order_and_eof_closes(self):
        import json
        import struct
        from threading import Thread
        from unittest.mock import patch

        from experiments.mutter_app.live import initialize, receive_click

        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            pair = root / 'pair'
            initialize(pair)
            (root / 'nested-window').write_text('42')

            def user_input():
                with (pair / 'input.fifo').open('wb', buffering=0) as fd:
                    fd.write(b'M' + struct.pack('!HH', 19, 45) + b'L' + struct.pack('!HH', 31, 68))

            writer = Thread(target=user_input)
            writer.start()
            with patch('experiments.mutter_app.live.subprocess.run') as apply:
                receive_click(root, pair, interactive=True)
            writer.join(2)
            self.assertEqual(
                [a.args[0] for a in apply.call_args_list],
                [
                    ['xdotool', 'mousemove', '--window', '42', '19', '45'],
                    ['xdotool', 'mousemove', '--window', '42', '31', '68', 'click', '1'],
                ],
            )
            self.assertEqual(
                json.loads((pair / 'last-input.json').read_text()), {'sequence': 2, 'applied': True}
            )
            self.assertTrue((root / 'controller-closed').is_file())

    def test_drawing_cache_evicts_by_bytes_and_lru_without_retiring_a_revisited_scene(self):
        import json

        from experiments.mutter_app.capture import DrawingCache

        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            from cloud_render.wire import Frame, Layer, encode_message

            def drawing(content):
                return Frame(1, 1090, 760, (Layer(0, 0, 1, 1, b'skiapict' + content),))

            small = len(encode_message(drawing(b'aa')))
            cache = DrawingCache(root, capacity=2, byte_limit=2 * small)
            cache.add('a', drawing(b'aa'), 10)
            cache.add('b', drawing(b'bb'), 20)
            self.assertTrue(cache.find('a'))
            cache.add('c', drawing(b'cc'), 30)
            self.assertEqual(list(json.loads((root / 'scenes.json').read_text())), ['a', 'c'])
            self.assertFalse(cache.find('b'))
            cache.add('d', drawing(b'd' * small), 40)
            self.assertEqual(list(cache.records), ['d'])
            self.assertEqual(len(list((root / 'drawings').glob('*.clrp'))), 1)
            self.assertEqual(cache.sequence, 4)

    def test_conflicting_recordings_revoke_both_claims_until_new_distinct_original_samples(self):
        import json
        from dataclasses import replace

        from cloud_render.wire import Frame, Layer, encode_message
        from experiments.mutter_app.capture import DrawingCache, command_digest

        a = Frame(1, 100, 100, (Layer(0, 0, 100, 100, b'skiapict-A'),))
        same = replace(a, sequence=2)
        b = replace(a, sequence=3, layers=(replace(a.layers[0], skp=b'skiapict-B'),))
        self.assertNotEqual(encode_message(a), encode_message(same))
        self.assertEqual(command_digest(a), command_digest(same))
        self.assertNotEqual(command_digest(a), command_digest(b))
        self.assertNotEqual(
            command_digest(a), command_digest(replace(a, layers=(replace(a.layers[0], x=1),)))
        )
        with TemporaryDirectory() as tmp:
            cache = DrawingCache(Path(tmp))

            def issue(rgb, frame, start):
                return cache.add(rgb, frame, start)

            self.assertTrue(issue('rgbA', a, 10))
            self.assertFalse(issue('rgbB', same, 20))
            self.assertEqual(json.loads((cache.out / 'scenes.json').read_text()), {})
            self.assertEqual(list(cache.path.iterdir()), [])
            self.assertFalse(issue('rgbA', replace(same, sequence=3), 30))
            self.assertFalse(issue('rgbB', replace(same, sequence=4), 40))
            self.assertTrue(issue('rgbB', b, 50))
            self.assertTrue(issue('rgbA', replace(a, sequence=6), 60))
            self.assertEqual(cache.records['rgbA']['witnessByteStart'], 60)
            self.assertEqual(cache.records['rgbB']['witnessByteStart'], 50)

    def test_unresolved_conflict_limit_cannot_grant_old_ambiguous_pictures(self):
        from dataclasses import replace

        from cloud_render.wire import Frame, Layer
        from experiments.mutter_app.capture import DrawingCache

        frame = Frame(1, 10, 10, (Layer(0, 0, 10, 10, b'skiapict-X'),))
        with TemporaryDirectory() as tmp:
            cache = DrawingCache(Path(tmp), capacity=1)
            self.assertTrue(cache.add('a', frame, 1))
            self.assertFalse(cache.add('b', replace(frame, sequence=2), 2))
            self.assertFalse(cache.add('c', replace(frame, sequence=3), 3))
            self.assertTrue(cache.saturated)
            self.assertFalse(cache.add('d', replace(frame, width=12), 4))
            self.assertEqual(cache.records, {})


class SourceEditorTest(unittest.IsolatedAsyncioTestCase):
    async def test_source_editor_ignores_unpresented_and_invalidated_focus(self):
        import io
        from types import SimpleNamespace
        from unittest.mock import AsyncMock

        from cloud_render.chatgpt_process import APP_PAGE
        from experiments.mutter_app.editor import FOCUS, Editor

        with TemporaryDirectory() as tmp:
            out = Path(tmp)
            events = io.BytesIO()
            page = SimpleNamespace(url=APP_PAGE, context=SimpleNamespace(pages=[object()], browser=None))
            editor = Editor(page, out, events)
            editor.focus = AsyncMock()
            editor.focus.matches.return_value = True
            self.assertFalse(await editor.check())
            (out / 'presented-scene.json').touch()
            self.assertTrue(await editor.check())
            self.assertEqual(events.getvalue(), FOCUS.pack(1, 1))
            page.url = 'about:blank'
            self.assertFalse(await editor.check())
            self.assertEqual(events.getvalue(), FOCUS.pack(1, 1) + FOCUS.pack(2, 0))

    async def test_delete_requires_same_editor_and_exact_focus_generation(self):
        import io
        from unittest.mock import AsyncMock, patch

        from experiments.mutter_app.editor import Editor

        with TemporaryDirectory() as tmp:
            editor = Editor(object(), Path(tmp), io.BytesIO())
            editor.focus = AsyncMock()
            editor.revision = 7
            editor.check = AsyncMock(return_value=True)
            for op, command in ((b'B', 'delete'), (b'D', 'forwardDelete')):
                with self.subTest(op=op):
                    with patch('experiments.mutter_app.editor.decode_input', return_value=(op, (6,))):
                        self.assertFalse(await editor.apply(op))
                    editor.focus.handle.evaluate.assert_not_awaited()
                    with patch('experiments.mutter_app.editor.decode_input', return_value=(op, (7,))):
                        self.assertTrue(await editor.apply(op))
                    self.assertEqual(editor.focus.handle.evaluate.await_args.args[1], [command, ''])
                    editor.focus.handle.evaluate.reset_mock()

    async def test_reference_deadlines_discard_incomplete_drawings_and_recover_without_new_input(self):
        import json
        from unittest.mock import AsyncMock, Mock, patch

        from cloud_render.chatgpt_process import APP_PAGE
        from cloud_render.wire import Frame, Layer
        from experiments.mutter_app.capture import SUPPORTED_GEOMETRY, DrawingCache, observe_interactive

        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            request = {'businessRgbSha256': 'scene', 'witnessByte': 13}
            (root / 'requested-scene.json').write_text(json.dumps(request))
            (root / 'owner.log').touch()
            page = Mock()
            page.url = APP_PAGE
            page.is_closed.return_value = False
            seen = []
            original_add = DrawingCache.add

            def added(cache, digest, frame, start):
                seen.append(digest)
                original_add(cache, digest, frame, start)
                (root / 'controller-closed').touch()
                return True

            async def backoff(seconds):
                self.assertEqual(seconds, 0.5)
                self.assertFalse((root / 'scenes.json').exists())
                self.assertEqual(json.loads((root / 'pending-scene.json').read_text()), {})

            with (
                patch('experiments.mutter_app.capture._exclusive_page', return_value=True),
                patch(
                    'experiments.mutter_app.capture.geometry', new=AsyncMock(return_value=SUPPORTED_GEOMETRY)
                ),
                patch(
                    'experiments.mutter_app.capture._reference_digest',
                    new=AsyncMock(
                        side_effect=[None, 'scene', None, 'scene', 'another phase', 'scene', 'scene']
                    ),
                ) as reference,
                patch(
                    'experiments.mutter_app.capture.capture_frame',
                    new=AsyncMock(return_value=Frame(1, 1090, 760, (Layer(0, 0, 1, 1, b'skiapict'),))),
                ) as capture,
                patch('experiments.mutter_app.capture.encode_message', return_value=b'complete original'),
                patch('experiments.mutter_app.capture.asyncio.sleep', new=backoff),
                patch.object(DrawingCache, 'add', new=added),
            ):
                await observe_interactive(page, root, ())
            self.assertEqual(seen, ['scene'])
            self.assertEqual(reference.await_count, 7)
            self.assertEqual(capture.await_count, 3)
            self.assertEqual(json.loads((root / 'unmatched-scene.json').read_text()), request)
            self.assertEqual(
                json.loads((root / 'capture-counts.json').read_text()),
                {'references': 7, 'traces': 3, 'referenceTimeouts': 2},
            )

    async def test_idle_original_surface_never_requests_an_application_screenshot(self):
        from unittest.mock import AsyncMock, Mock, patch

        from experiments.mutter_app.capture import observe_interactive

        with TemporaryDirectory() as tmp:
            out = Path(tmp)
            page = Mock()
            page.is_closed.return_value = False
            page.screenshot = AsyncMock()

            async def finish(_):
                (out / 'controller-closed').touch()

            with patch('experiments.mutter_app.capture.asyncio.sleep', side_effect=finish):
                await observe_interactive(page, out, ())
            page.screenshot.assert_not_awaited()

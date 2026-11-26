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
                    out.write('GL 1 12 3553\nEND 1\nORIGIN 476 UNCLASSIFIED_SHM 8 0 0 10 9 360\n')
                kind, desc, metadata = witness.at(64)
                self.assertEqual((kind, desc), ('SHM', [5, 0, 0, 10, 9, 360]))
                self.assertEqual(metadata['gl'], (12, 3553))
                self.assertEqual(witness.at(476)[0], 'UNCLASSIFIED_SHM')
                self.assertIsNone(witness.at(64))
            finally:
                witness.file.close()

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

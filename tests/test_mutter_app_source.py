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

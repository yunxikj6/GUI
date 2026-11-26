import hashlib
import struct
import unittest
from unittest.mock import patch

from cloud_render import skp_fonts as fonts


PREFIX = b'skiapict' + struct.pack('<I4fB', 110, 0, 0, 640, 480, 1)
PREFIX += b'daer\x00\x00\x00\x00tcaf\x04\x00\x00\x00\x00\x00\x00\x00cfpt'
TAIL = b'yara' + struct.pack('<I', 0) + b' foe'


def descriptor(family=b'DejaVu Sans', extra=b''):
    raw = b'\xff' + struct.pack('<I', 400 << 16 | 5 << 8)
    raw += bytes((1, len(family))) + family
    for tag, value in ((16, 400), (17, 100), (18, 0), (19, 0)):
        raw += bytes([tag]) + struct.pack('<f', value)
    return raw + extra + b'\xfc\xffatnf\xfe\xff\x00'


def picture(*entries):
    return PREFIX + struct.pack('<I', len(entries)) + b''.join(entries) + TAIL


class SkpFonts(unittest.TestCase):
    def test_preserves_no_font_and_resolves_repeated_verified_font_once(self):
        self.assertEqual(fonts.embed_typefaces(picture()), picture())
        stub = b'font-fixture' * 6000
        face = ('DejaVu Sans', 400)
        desc = descriptor()
        complete = desc + b'\xff' + struct.pack('<I', len(stub)) + stub
        pins = {face: ('unused', {hashlib.sha256(stub).hexdigest()})}
        with patch.dict(fonts._FACES, pins), patch.object(fonts, '_system_font', return_value=stub) as resolve:
            expanded = fonts.embed_typefaces(picture(desc + b'\0', desc + b'\0'))
            self.assertEqual(expanded, picture(complete, complete))
            self.assertEqual(fonts.embed_typefaces(expanded), expanded)
            resolve.assert_called_once_with(face)

    def test_unknown_embedded_fonts_names_and_truncation_fail_closed(self):
        desc = descriptor()
        for raw in [picture(desc + b'\x03abc'), picture(desc[:-5]),
                    picture(descriptor(b'../system-data') + b'\0'),
                    picture(descriptor(extra=b'\x06\x04Fake') + b'\0'),
                    picture(descriptor(extra=b'\xf6') + b'\0'),
                    picture(descriptor(extra=b'\x01\x01A') + b'\0'),
                    picture()[:-1], picture() + b'extra', picture().replace(b'tcaf', b'fake')]:
            with self.subTest(raw=raw[:48]), patch.object(fonts, '_system_font') as resolve:
                with self.assertRaises(ValueError):
                    fonts.embed_typefaces(raw)
                resolve.assert_not_called()

    def test_file_with_same_name_and_different_glyphs_is_refused(self):
        with patch.object(fonts, '_system_font', return_value=b'other'):
            with self.assertRaises(fonts.FontError):
                fonts.embed_typefaces(picture(descriptor() + b'\0'))

import hashlib
from pathlib import Path
import struct
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

from cloud_render import skp_fonts as fonts


PREFIX = b'skiapict' + struct.pack('<I4fB', 110, 0, 0, 640, 480, 1)
PREFIX += b'daer\x00\x00\x00\x00tcaf\x04\x00\x00\x00\x00\x00\x00\x00cfpt'


def descriptor(family=b'DejaVu Sans', extra=b''):
    raw = b'\xff' + struct.pack('<I', 400 << 16 | 5 << 8)
    raw += bytes((1, len(family))) + family
    for tag, value in ((16, 400), (17, 100), (18, 0), (19, 0)):
        raw += bytes([tag]) + struct.pack('<f', value)
    return raw + extra + b'\xfc\xffatnf\xfe\xff\x00'


def picture(*entries, resources=b''):
    return (PREFIX + struct.pack('<I', len(entries)) + b''.join(entries) +
            b'yara' + struct.pack('<I', len(resources)) + resources + b' foe')


def array(data):
    return struct.pack('<I', len(data)) + data + bytes(-len(data) % 4)


def glyph_run(index, ids, position=0, extended=b'', font_flags=0x90000000):
    raw = struct.pack('<II', len(ids), position | (0x100 if extended else 0))
    if extended:
        raw += struct.pack('<I', len(extended))
    raw += bytes(8) + struct.pack('<I', font_flags)
    if not font_flags & 1 << 31:
        raw += struct.pack('<f', 16)
    if font_flags & 1 << 30:
        raw += struct.pack('<f', 1)
    if font_flags & 1 << 29:
        raw += struct.pack('<f', 0.5)
    if font_flags & 1 << 28:
        raw += struct.pack('<I', index)
    raw += array(struct.pack(f'<{len(ids)}H', *ids)) + array(bytes((0, 4, 8, 16)[position] * len(ids)))
    if extended:
        raw += array(bytes(len(ids) * 4)) + array(extended)
    return raw


def blobs(*runs):
    return b'bolb' + struct.pack('<I', 1) + bytes(16) + b''.join(runs) + bytes(4) + b'guls' + bytes(4)


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

    def test_collection_uses_only_recorded_glyphs_and_rechecks_already_embedded_subset(self):
        desc = descriptor(b'Noto Sans CJK JP')
        runs = blobs(glyph_run(1, [16644, 9980], position=1, extended='工作'.encode()),
                     glyph_run(1, [11633, 9980], position=3, font_flags=0x70000000))
        subset = b'OTTO-test-only'
        with (patch.object(fonts, '_cjk_source', return_value=b'pinned source'),
              patch.object(fonts, '_subset_collection', return_value=subset) as make):
            result = fonts.embed_typefaces(picture(desc + b'\0', resources=runs))
            make.assert_called_once_with(b'pinned source', (9980, 11633, 16644))
            self.assertEqual(result, fonts.embed_typefaces(result))
            with self.assertRaises(fonts.FontError):
                fonts.embed_typefaces(result.replace(subset, b'x' * len(subset)))

    def test_collection_table_skips_effect_path_and_encoded_images_by_length(self):
        # These arbitrary data bytes look like text tags but are image/paint data.
        effect = struct.pack('<II', 1, 4) + b'bolb' + bytes(20)
        paint = b' tnp' + struct.pack('<I', 1) + bytes(24) + struct.pack('<I', 2 << 24) + effect
        paths = b' htp' + struct.pack('<III', 2, 2, 5) + struct.pack('<III', 1, 0, 1) + bytes(12)
        paths += struct.pack('<I', 1 << 28 | 5) + bytes(52)
        images = b'gami' + struct.pack('<II', 1, 0x700) + array(b'bolbX') + bytes(16) + array(b'guls')
        fonts_used = [('DejaVu Sans', 400), fonts._CJK_FACE]
        runs = blobs(glyph_run(1, [999]), glyph_run(2, [7, 71], position=2))
        self.assertEqual(fonts._collection_glyphs(paint + paths + runs + images, fonts_used, 1), {2: {7, 71}})

    def test_collection_rejects_truncated_oversize_ambiguous_and_out_of_range_resources(self):
        desc = descriptor(b'Noto Sans CJK JP') + b'\0'
        run = glyph_run(1, [1234])
        resources = blobs(run)
        cases = [
            resources[:-1], resources + resources, resources + b'gami' + struct.pack('<II', 1, 0xFFFFFFFF),
            blobs(glyph_run(2, [1234])), blobs(glyph_run(1, [1234], font_flags=0x80000000)),
            blobs(struct.pack('<II', fonts._MAX_GLYPHS + 1, 0)),
            blobs(run[:4] + struct.pack('<I', 0x200) + run[8:]),
            blobs(run[:24] + struct.pack('<I', 1) + run[28:]),
            blobs(glyph_run(1, list(range(fonts._MAX_SUBSET_GLYPHS + 1)))),
            b' tnp' + struct.pack('<I', 1) + bytes(24) + struct.pack('<I', 4 << 24) + resources,
            b' htp' + struct.pack('<II', 1, 0) + resources,
            b'\0\0\0\0' + resources,
        ]
        for i, data in enumerate(cases):
            with self.subTest(case=i), patch.object(fonts, '_cjk_source') as source:
                with self.assertRaises(fonts.FontError):
                    fonts.embed_typefaces(picture(desc, resources=data))
                source.assert_not_called()
        with self.assertRaises(fonts.FontError):
            fonts._text_free_factories(struct.pack('<I', 1) + b'\x0fSkPictureShader')

    def test_collection_validates_source_bytes_index_and_retained_gid_map(self):
        with (tempfile.TemporaryDirectory() as folder,
              patch.object(fonts, '_CJK_FILE', Path(folder) / 'sample.ttc'),
              patch.object(fonts, '_CJK_SIZE', 4),
              patch.object(fonts, '_CJK_SHA256', hashlib.sha256(b'GOOD').hexdigest()),
              patch.object(fonts.subprocess, 'run') as match):
            fonts._CJK_FILE.write_bytes(b'GOOD')
            match.return_value.stdout = f'{fonts._CJK_FILE}\n2\n'
            with self.assertRaises(fonts.FontError):
                fonts._cjk_source()
            match.return_value.stdout = f'{fonts._CJK_FILE}\n0\n'
            self.assertEqual(fonts._cjk_source(), b'GOOD')
            fonts._CJK_FILE.write_bytes(b'FAKE')
            with self.assertRaises(fonts.FontError):
                fonts._cjk_source()
        hb = Mock()
        hb.Face.return_value.glyph_count = 100
        hb.SubsetFlags.RETAIN_GIDS = 2
        hb.SubsetFlags.NO_LAYOUT_CLOSURE = 512
        hb.SubsetPlan.return_value.old_to_new_glyph_mapping = {8: 1}
        with patch.dict(sys.modules, {'uharfbuzz': hb}):
            for ids in [(100,), (8,)]:
                with self.assertRaises(fonts.FontError):
                    fonts._subset_collection(b'test', ids)
            hb.SubsetPlan.return_value.execute.assert_not_called()

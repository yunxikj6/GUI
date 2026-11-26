"""Make Chromium v110 trace pictures portable using verified system fonts.

Chromium's PictureDebugUtil can emit SkFontDescriptors without font data. Skia
stores glyph IDs in the drawing commands, so a similar substitute cannot render
these pictures reliably. Only the listed exact files can be sent to the client.
The source process and this module must share the same fontconfig environment.
"""
import hashlib
from pathlib import Path
import struct
import subprocess

from .wire import MAX_PICTURE


class FontError(ValueError):
    """The picture's typefaces cannot be reproduced by the local Skia build."""


# SHA-256 pins for the complete distro font files, preserving the glyph-ID map.
# DejaVu is from Ubuntu Noble and Debian Trixie, Liberation from Debian Trixie.
# Their redistribution notices accompany the renderer in thin_client/vendor/.
_FACES = {
    ('DejaVu Sans', 400): ('dejavu/DejaVuSans.ttf', {
        'ae7b7855e115a5966d8b1b3f80f254ccc117ec86f9965e202ee2940453837280',
        '57f73e11f51999432bf7ab22ce55b6f945d5eca1bf824404cfa9ec2e3718c84e'}),
    ('DejaVu Sans', 700): ('dejavu/DejaVuSans-Bold.ttf', {
        '5c1247acef7f2b8522a31742c76d6adcb5569bacc0be7ceaa4dc39dd252ce895',
        'a4c5bc453ca281d90ea079e596da7ae0dfeb5777497c29ec254e76d97ff6f890'}),
    ('Liberation Sans', 400): ('liberation/LiberationSans-Regular.ttf', {
        'bade59d822652f76e6941aa87b40a87c13d1cc70db98ededb5011127efafd1d3'}),
    ('Liberation Sans', 700): ('liberation/LiberationSans-Bold.ttf', {
        '1b5f2da6f4cadce4c05b9ecebe3a6fcd374eb95ae443605e799f4c3287978939'}),
    ('Liberation Serif', 400): ('liberation/LiberationSerif-Regular.ttf', {
        '9caef765d2e891c10dd73658894f01e660fe0c1c83e0bda7d6edf561d8f623d4'}),
    ('Liberation Serif', 700): ('liberation/LiberationSerif-Bold.ttf', {
        '443a4dbd694b6e4689925947b456e0532e10b19c90fa32eb39ea6c2cd9db9ebd'}),
}
_MAX_FONT = 2 * 1024 * 1024


class _Reader:
    def __init__(self, raw):
        self.raw = memoryview(raw)
        self.pos = 0

    def take(self, count):
        if count > len(self.raw) - self.pos:
            raise FontError('Skia 绘图资源不完整。')
        start = self.pos
        self.pos += count
        return self.raw[start:self.pos]

    def u32(self):
        return int.from_bytes(self.take(4), 'little')

    def packed(self):
        tag = self.take(1)[0]
        if tag == 0xfe:
            return int.from_bytes(self.take(2), 'little')
        return self.u32() if tag == 0xff else tag

    def expect(self, tag):
        if self.take(len(tag)) != tag:
            raise FontError('无法识别 Skia 绘图资源格式。')


def _descriptor(reader):
    bits = reader.packed()
    fields = {}
    while (key := reader.packed()) != 0xff:
        if key in fields:
            raise FontError('Skia 字体字段重复。')
        if key in (1, 4, 6):  # family, full and PostScript names
            size = reader.packed()
            if size > 512:
                raise FontError('字体名称过长。')
            fields[key] = bytes(reader.take(size)).decode('utf-8')
        elif 0x10 <= key <= 0x13:
            fields[key], = struct.unpack('<f', reader.take(4))
        elif key == 0xfc:
            fields[key] = reader.packed()
        else:
            # This version only supports static, non-collection regular or bold
            # fonts. Variable axes, collections and synthetic styles need their
            # own validated source mapping, even when the family is familiar.
            raise FontError('尚未支持此种字体描述。')
    weight = fields.get(0x10)
    face = fields.get(1), weight
    if (face not in _FACES or bits != (int(weight) << 16 | 5 << 8) or
            fields.get(0x11) != 100 or fields.get(0x12) != 0 or fields.get(0x13) != 0 or
            fields.get(0xfc) != int.from_bytes(b'fnta', 'big')):
        raise FontError('此窗口使用了尚未验证的字体或字形样式。')
    family = face[0]
    full = family + (' Bold' if weight == 700 else '')
    postscript = family.replace(' ', '') + ('-Bold' if weight == 700 else '')
    if fields.get(4, full) != full or fields.get(6, postscript) != postscript:
        raise FontError('字体的完整名称与已验证文件不匹配。')
    return face


def _system_font(face):
    family, weight = face
    name, _ = _FACES[face]
    expected = Path('/usr/share/fonts/truetype') / name
    style = 'bold' if weight == 700 else 'regular'
    try:
        match = subprocess.run(['/usr/bin/fc-match', '--format=%{file}\\n', f'{family}:weight={style}'],
                               capture_output=True, check=True, timeout=4, text=True)
        if match.stdout.rstrip('\n') != str(expected) or expected.resolve() != expected:
            raise FontError('云端所选字体与已验证的安装文件不符。')
        with expected.open('rb') as font:
            data = font.read(_MAX_FONT + 1)
    except (OSError, subprocess.SubprocessError) as exc:
        raise FontError('无法核对云端系统字体。') from exc
    return data


def embed_typefaces(raw):
    """Hydrate a v110 picture's top-level typeface table; preserve all draw ops."""
    if len(raw) > MAX_PICTURE:
        raise FontError('绘图已超过传输上限。')
    reader = _Reader(raw)
    reader.expect(b'skiapict' + struct.pack('<I', 110))
    reader.take(16)  # SkRect cull bounds
    reader.expect(b'\x01')  # standard serialized SkPictureData
    for section in (b'daer', b'tcaf'):  # operations and factories are length-prefixed
        reader.expect(section)
        reader.take(reader.u32())
    reader.expect(b'cfpt')
    count = reader.u32()
    if count > 128:
        raise FontError('绘图引用的字体过多。')
    parts = []
    last = 0
    length = len(raw)
    resolved = {}
    for _ in range(count):
        face = _descriptor(reader)
        offset = reader.pos
        size = reader.packed()
        if size > _MAX_FONT:
            raise FontError('字体超过资源上限。')
        if size:
            data = reader.take(size)
        else:
            if face not in resolved:
                resolved[face] = _system_font(face)
            data = resolved[face]
        if len(data) > _MAX_FONT or hashlib.sha256(data).hexdigest() not in _FACES[face][1]:
            raise FontError('云端字体文件未通过完整性校验。')
        if not size:
            # All pinned fonts are larger than 65535 bytes (SkStream packed U32).
            packed = b'\xff' + struct.pack('<I', len(data))
            length += len(data) + len(packed) - (reader.pos - offset)
            if length > MAX_PICTURE:
                raise FontError('补全字体后的绘图已超过传输上限。')
            parts.extend((reader.raw[last:offset], packed, data))
            last = reader.pos
    reader.expect(b'yara')  # Packed drawing buffers follow the top-level font table.
    reader.take(reader.u32())
    # Extra (nested/custom) pictures require separate format validation before
    # their resources can be shared. The supported Chromium compositor emits
    # independent layers on the wire and ends each with SK_PICT_EOF_TAG.
    reader.expect(b' foe')
    if reader.pos != len(raw):
        raise FontError('Skia 绘图尾部含有未验证的资源。')
    if not parts:
        return raw
    parts.append(reader.raw[last:])
    return b''.join(parts)

"""Make Chromium v110 trace pictures portable using verified system fonts.

Chromium's PictureDebugUtil can emit SkFontDescriptors without font data. Skia
stores glyph IDs in the drawing commands, so a similar substitute cannot render
these pictures reliably. Large pinned collections are subset by recorded glyph
ID while preserving those indices and their outlines.
The source process and this module must share the same fontconfig environment.
"""
from functools import lru_cache
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
    ('Noto Sans', 700): ('noto/NotoSans-Bold.ttf', {
        'e83493c945848ecd4a9ad0f6d19164541a0d3e23a9c952304a00a46e00272ac5'}),
    ('Noto Sans', 400): ('noto/NotoSans-Regular.ttf', {
        '89c3c497f618fdaa0b2d1e98fef93582f28c71debd2c4a8cdf41f190ced2909d'}),
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
_CJK_FACE = ('Noto Sans CJK JP', 400)
_CJK_FILE = Path('/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc')
_CJK_SHA256 = 'b76b0433203017ca80401b2ee0dd69350349871c4b19d504c34dbdd80541690a'
_CJK_SIZE = 19_484_784
_MAX_RESOURCES = 4096
_MAX_GLYPHS = 131072
_MAX_SUBSET_GLYPHS = 4096


class _Reader:
    def __init__(self, raw):
        self.raw = memoryview(raw)
        self.pos = 0

    def take(self, count):
        if count < 0 or count > len(self.raw) - self.pos:
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

    def count(self):
        count = self.u32()
        if count > _MAX_RESOURCES:
            raise FontError('绘图资源表超过数量上限。')
        return count

    def array(self, size):
        if self.u32() != size:
            raise FontError('Skia 字形绘图数组与声明的元素数量不一致。')
        data = self.take(size)
        self.take(-size % 4)
        return data


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
    if ((face not in _FACES and face != _CJK_FACE) or bits != (int(weight) << 16 | 5 << 8) or
            fields.get(0x11) != 100 or fields.get(0x12) != 0 or fields.get(0x13) != 0 or
            fields.get(0xfc) != int.from_bytes(b'fnta', 'big')):
        raise FontError('此窗口使用了尚未验证的字体或字形样式。')
    family = face[0]
    full = family + (' Bold' if weight == 700 else '')
    postscript = ('NotoSansCJKjp-Regular' if face == _CJK_FACE else
                  family.replace(' ', '') + ('-Bold' if weight == 700 else ''))
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


def _skip_paints(reader, factories):
    for _ in range(reader.count()):
        reader.take(24)  # stroke width, miter and RGBA
        effects = reader.u32() >> 24
        if effects == 0:
            continue
        if effects != 2:
            raise FontError('尚未支持此种 Skia 画笔。')
        for _ in range(6):  # v110 path effect, shader, mask/color/image filters and blender
            factory = reader.u32()
            if factory:
                size = reader.u32()
                if factory > factories or size % 4:
                    raise FontError('无效的 Skia 画笔资源。')
                reader.take(size)


def _text_free_factories(data):
    # A picture shader/filter can itself refer to text outside the top-level
    # blob table. Only these vetted, font-independent effects may be skipped.
    reader = _Reader(data)
    count = reader.count()
    for _ in range(count):
        name = reader.take(reader.packed())
        if name not in (b'SkColorFilterImageFilterImpl', b'SkMatrixColorFilter'):
            raise FontError('画笔含有尚未确认字体引用方式的效果。')
    if reader.pos != len(data):
        raise FontError('Skia 画笔类型表包含额外数据。')
    return count


def _skip_paths(reader):
    count = reader.count()
    if reader.u32() != count:
        raise FontError('Skia 路径数量不一致。')
    for _ in range(count):
        flags = reader.u32()
        kind = flags >> 28
        if flags & 0xff != 5 or kind not in (0, 1):
            raise FontError('尚未支持此种 Skia 路径。')
        if kind == 1:  # SkRRect bounds, four corner radii and start index
            reader.take(52)
        else:
            points, conics, verbs = reader.u32(), reader.u32(), reader.u32()
            reader.take(points * 8 + conics * 4 + verbs + (-verbs % 4))


def _run_typeface(reader):
    reader.take(8)  # run origin
    flags = reader.u32()
    if not flags & (1 << 31):
        reader.take(4)  # float size
    if flags & (1 << 30):
        reader.take(4)  # scaleX
    if flags & (1 << 29):
        reader.take(4)  # skewX
    if not flags & (1 << 28):
        raise FontError('Skia 文字缺少明确的字体索引。')
    return reader.u32()


def _collection_glyphs(resources, faces, factories, *, vector_only=False):
    # Walk the SkPictureData resource table from its start. A paint/effect/path
    # can contain arbitrary bytes, including tags that look like a text blob.
    reader = _Reader(resources)
    tag = reader.take(4)
    if tag == b' tnp':
        _skip_paints(reader, factories)
        tag = reader.take(4)
    if tag == b' htp':
        _skip_paths(reader)
        tag = reader.take(4)
    if tag == b'bolb':
        blobs = reader.count()
    elif vector_only and not faces and tag == b'guls':
        if reader.u32() or reader.pos != len(resources):
            raise FontError('Skia 矢量图层含有额外资源。')
        return {}
    else:
        raise FontError('未找到包含所需字形的 Skia 文字记录。')
    glyphs = {i: set() for i, face in enumerate(faces, 1) if face == _CJK_FACE}
    total = 0
    for _ in range(blobs):
        reader.take(16)  # text blob bounds
        while (count := reader.u32()):
            total += count
            position = reader.u32()
            if total > _MAX_GLYPHS or position & ~0x103:
                raise FontError('Skia 文字记录超出支持范围。')
            extended = position & 0x100
            text_bytes = reader.u32() if extended else 0
            index = _run_typeface(reader)
            if not 1 <= index <= len(faces):
                raise FontError('Skia 字形字体索引无效。')
            ids = reader.array(count * 2)
            if index in glyphs:
                glyphs[index].update(g for g, in struct.iter_unpack('<H', ids))
                if len(glyphs[index]) > _MAX_SUBSET_GLYPHS:
                    raise FontError('当前窗口字体子集超过字形数量上限。')
            reader.array(count * (0, 4, 8, 16)[position & 3])
            if extended:
                reader.array(count * 4)  # cluster map
                reader.array(text_bytes)
    reader.expect(b'guls')
    if reader.u32() or not all(glyphs.values()):
        raise FontError('Skia 文字资源含有尚未验证的字形引用。')
    if vector_only and reader.pos != len(resources):
        raise FontError('Skia 矢量图层含有额外资源。')
    if reader.pos < len(resources):
        # Encoded image assets cannot refer to Skia font indices.
        reader.expect(b'gami')
        for _ in range(reader.count()):
            flags = reader.u32()
            if flags & ~0x700:
                raise FontError('未知的 Skia 图像资源标记。')
            size = reader.u32()
            reader.take(size + (-size % 4))
            if flags & 0x100:
                reader.take(16)
            if flags & 0x200:
                mip_size = reader.u32()
                reader.take(mip_size + (-mip_size % 4))
    if reader.pos != len(resources):
        raise FontError('Skia 文字表后含有未经验证的资源。')
    return glyphs


@lru_cache(maxsize=1)
def _read_collection(file_identity):
    # The stat identity invalidates the cache on replacement or modification.
    with _CJK_FILE.open('rb') as source:
        data = source.read(_CJK_SIZE + 1)
    if len(data) != _CJK_SIZE or hashlib.sha256(data).hexdigest() != _CJK_SHA256:
        raise FontError('云端字体集合未通过完整性校验。')
    return data


def _cjk_source():
    try:
        result = subprocess.run(['/usr/bin/fc-match', '--format=%{file}\\n%{index}\\n',
                                 'Noto Sans CJK JP:weight=regular'], capture_output=True,
                                text=True, check=True, timeout=4)
        if result.stdout.splitlines() != [str(_CJK_FILE), '0'] or _CJK_FILE.resolve() != _CJK_FILE:
            raise FontError('云端所选字体集合或字面索引与已验证文件不符。')
        info = _CJK_FILE.stat()
        return _read_collection((info.st_dev, info.st_ino, info.st_mtime_ns, info.st_ctime_ns, info.st_size))
    except (OSError, subprocess.SubprocessError) as exc:
        raise FontError('无法核对云端字体集合。') from exc


@lru_cache(maxsize=4)
def _subset_collection(source, glyph_ids):
    try:
        import uharfbuzz as hb
    except ImportError as exc:
        raise FontError('云端缺少 uharfbuzz 字体子集依赖。') from exc
    face = hb.Face(source, 0)
    if not glyph_ids or max(glyph_ids) >= face.glyph_count:
        raise FontError('字形索引超出原始字体范围。')
    options = hb.SubsetInput()
    options.flags = hb.SubsetFlags.RETAIN_GIDS | hb.SubsetFlags.NO_LAYOUT_CLOSURE
    options.glyph_set.update(glyph_ids)
    options.name_id_set.update((13, 14))  # retain the license in the font as well
    try:
        plan = hb.SubsetPlan(face, options)
        mapping = plan.old_to_new_glyph_mapping
        if any(mapping.get(i) != i for i in glyph_ids):
            raise FontError('字体子集未能保留原始字形索引。')
        data = plan.execute().blob.data
    except RuntimeError as exc:
        raise FontError('无法生成原始字形子集。') from exc
    if len(data) > _MAX_FONT or not data.startswith(b'OTTO'):
        raise FontError('字体子集超过资源上限或格式不受支持。')
    return data


def embed_typefaces(raw):
    """Hydrate a v110 picture's top-level typeface table; preserve all draw ops."""
    if len(raw) > MAX_PICTURE:
        raise FontError('绘图已超过传输上限。')
    reader = _Reader(raw)
    reader.expect(b'skiapict' + struct.pack('<I', 110))
    reader.take(16)  # SkRect cull bounds
    reader.expect(b'\x01')  # standard serialized SkPictureData
    reader.expect(b'daer')
    reader.take(reader.u32())
    reader.expect(b'tcaf')
    factory_data = reader.take(reader.u32())
    reader.expect(b'cfpt')
    count = reader.u32()
    if count > 128:
        raise FontError('绘图引用的字体过多。')
    entries = []
    for _ in range(count):
        face = _descriptor(reader)
        offset = reader.pos
        size = reader.packed()
        if size > _MAX_FONT:
            raise FontError('字体超过资源上限。')
        embedded = reader.take(size)
        entries.append((face, offset, reader.pos, embedded))
    reader.expect(b'yara')  # Packed drawing buffers follow the top-level font table.
    resources = reader.take(reader.u32())
    # Extra (nested/custom) pictures require separate format validation before
    # their resources can be shared. The supported Chromium compositor emits
    # independent layers on the wire and ends each with SK_PICT_EOF_TAG.
    reader.expect(b' foe')
    if reader.pos != len(raw):
        raise FontError('Skia 绘图尾部含有未验证的资源。')
    faces = [entry[0] for entry in entries]
    glyphs = {}
    if _CJK_FACE in faces:
        glyphs = _collection_glyphs(resources, faces, _text_free_factories(factory_data))
        source = _cjk_source()
    parts = []
    last = 0
    length = len(raw)
    resolved = {}
    for i, (face, offset, end, embedded) in enumerate(entries, 1):
        if i in glyphs:
            data = _subset_collection(source, tuple(sorted(glyphs[i])))
            if embedded and embedded != data:
                raise FontError('嵌入字形与已验证的源字体子集不匹配。')
        else:
            if not embedded and face not in resolved:
                resolved[face] = _system_font(face)
            data = embedded or resolved[face]
            if len(data) > _MAX_FONT or hashlib.sha256(data).hexdigest() not in _FACES[face][1]:
                raise FontError('云端字体文件未通过完整性校验。')
        if not embedded:
            # SkStream accepts its five-byte packed U32 representation at all sizes.
            packed = b'\xff' + struct.pack('<I', len(data))
            length += len(data) + len(packed) - (end - offset)
            if length > MAX_PICTURE:
                raise FontError('补全字体后的绘图已超过传输上限。')
            parts.extend((reader.raw[last:offset], packed, data))
            last = end
    if not parts:
        return raw
    parts.append(reader.raw[last:])
    return b''.join(parts)

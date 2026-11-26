"""Translate Chromium Viz render passes into standard SkPicture v110 commands.

Only source pictures, compositor rectangles and paint state enter the recording.
Tile textures must resolve uniquely to an active PictureLayer's SkPicture via
its raster origin, dimensions, transform and display-list identity.
"""
from collections import defaultdict
from enum import IntEnum
import math
import re
import struct

from .wire import MAX_PICTURE


class VizError(ValueError):
    pass


class VizPending(VizError):
    """The renderer is between diagnostic paints or tile commits."""


class _Draw(IntEnum):
    CLIP_RECT = 3
    CLIP_RRECT = 4
    PICTURE = 15
    RECT = 21
    RESTORE = 28
    SAVE = 30
    SCALE = 32
    TRANSLATE = 35
    SAVE_LAYER = 52


class _Material(IntEnum):
    DEBUG_BORDER = 1
    RENDER_PASS = 3
    SOLID_COLOR = 5
    TILED_CONTENT = 10


class _Recording:
    # Skia's SkPictureFlat.h DrawType and SkPictureData.h chunk layout (v110).
    def __init__(self):
        self.ops = bytearray()
        self.paints = {}
        self.pictures = {}
        self.picture_bytes = 0

    def op(self, code, fmt='', *args):
        payload = struct.pack('<' + fmt, *args)
        self.ops.extend(struct.pack('<I', (code << 24) | (4 + len(payload))))
        self.ops.extend(payload)

    def paint(self, color, blend='SrcOver'):
        mode = {'SrcOver': 3, 'DstIn': 6}[blend]
        record = struct.pack('<6fI', 0, 4, *color, mode << 8)
        return self.paints.setdefault(record, len(self.paints) + 1)

    def picture(self, raw):
        if raw not in self.pictures:
            self.picture_bytes += len(raw)
            if self.picture_bytes > MAX_PICTURE:
                raise VizError('合成绘图及字体超过单帧指令上限。')
            self.pictures[raw] = len(self.pictures) + 1
        self.op(_Draw.PICTURE, 'I', self.pictures[raw])

    def clip(self, rect):
        self.op(_Draw.CLIP_RECT, '4fII', *_edges(rect), 1, 0)  # intersect; no indexed skip

    def transform(self, matrix):
        sx, sy, tx, ty = _affine(matrix)
        if tx or ty:
            self.op(_Draw.TRANSLATE, '2f', tx, ty)
        if sx != 1 or sy != 1:
            self.op(_Draw.SCALE, '2f', sx, sy)

    def finish(self, width, height):
        def tag(name, length):
            return name[::-1] + struct.pack('<I', length)
        buffer = tag(b'pnt ', len(self.paints)) + b''.join(self.paints) + tag(b'slug', 0)
        data = (b'skiapict' + struct.pack('<I4fB', 110, 0, 0, width, height, 1)
                + tag(b'read', len(self.ops)) + self.ops + tag(b'fact', 4) + bytes(4)
                + tag(b'tpfc', 0) + tag(b'aray', len(buffer)) + buffer)
        if self.pictures:
            data += tag(b'pctr', len(self.pictures)) + b''.join(self.pictures)
        if len(data) + 4 > MAX_PICTURE:
            raise VizError('合成绘图及字体超过单帧指令上限。')
        return data + b' foe'


def _require(ok):
    if not ok:
        raise VizError('当前 Viz 图面的合成、资源或裁剪条件无法准确重绘。')


def _numbers(seq, n):
    _require(isinstance(seq, (tuple, list)) and len(seq) == n and
             all(type(x) in (float, int) and math.isfinite(x) and abs(x) <= 65536 for x in seq))
    return seq


def _edges(rect):
    x, y, w, h = _numbers(rect, 4)
    _require(w >= 0 and h >= 0)
    return x, y, x + w, y + h


def _affine(m):
    _numbers(m, 16)
    _require(m == [m[0], 0, 0, m[3], 0, m[5], 0, m[7], 0, 0, 1, 0, 0, 0, 0, 1]
             and m[0] > 0 and m[5] > 0)
    return m[0], m[5], m[3], m[7]


def _close(a, b):
    return len(a) == len(b) and all(abs(x - y) <= .002 for x, y in zip(a, b))


def _tile_source(layers, tile_origins, render_pass, state, quad):
    sx, sy, tx, ty = _affine(state['quad_to_target_transform'])
    px, py, ox, oy = _affine(render_pass['transform_to_root_target'])
    x, y, r, b = _edges(state['quad_layer_rect'])
    left, top = (sx * x + tx) * px + ox, (sy * y + ty) * py + oy
    right, bottom = (sx * r + tx) * px + ox, (sy * b + ty) * py + oy
    rect, tex = quad['rect'], quad['tex_coord_rect']
    _require(quad.get('resource_id') and not quad.get('nearest_neighbor'))
    if not _close(rect[2:], tex[2:]):
        # During a transition Viz can already be applying this commit's
        # transform while sampling a tile at the previous raster scale.
        raise VizPending('合成器尚在替换缩放过渡的分块绘图。')
    # Raster tiles have an overlap at their edges. Viz's texture-coordinate
    # origin includes that border. Match the actual source tile origin.
    origin = (round(rect[0] - tex[0] - x), round(rect[1] - tex[1] - y))
    _require(_close(origin, [rect[0] - tex[0] - x, rect[1] - tex[1] - y]))
    ids = {t['layer_id'] for t in tile_origins[origin]
           if tex[0] + tex[2] <= t['content_rect'][2] + .002
           and tex[1] + tex[3] <= t['content_rect'][3] + .002}
    corners = [left, top, right, top, right, bottom, left, bottom]
    matches = [(layer_id, picture['id_ref']) for layer_id in ids if layer_id in layers
               and _close(layers[layer_id]['layer_quad'], corners)
               for picture in layers[layer_id].get('pictures', ())]
    if not matches:
        raise VizPending('活动图面尚未提交对应的分块绘图。')
    _require(len(matches) == 1)
    return matches[0]


_NUMBER = r'(-?\d+(?:\.\d+)?)'
_ROUND = re.compile(r'MaskFilterInfo\{' + _NUMBER + ',' + _NUMBER + ' ' + _NUMBER + 'x' +
                    _NUMBER + ', radius ' + _NUMBER + r'(?:, clip_id=\d+)?\}')
_RGBA = re.compile(r'rgba\(' + ', '.join([_NUMBER] * 4) + r'\)?')


def _mask(rec, state):
    mask = state['mask_filter_info']
    if not state['is_fast_rounded_corner']:
        _require(mask == 'MaskFilterInfo{0.000,0.000 0.000x0.000, rectangular}')
        return
    found = _ROUND.fullmatch(mask)
    _require(found)
    x, y, w, h, radius = map(float, found.groups())
    _require(0 <= radius <= min(w, h) / 2)
    rec.op(_Draw.CLIP_RRECT, '12fII', *_edges([x, y, w, h]), *([radius] * 8), 17, 0)


def compose(scene, width, height, source):
    """Compile the selected renderer's final pass DAG. source(ref) returns x,y,SKP."""
    passes = scene['frame']['render_passes']
    _require(1 <= len(passes) <= 64 and passes[-1]['output_rect'] == [0, 0, width, height])
    by_id = {p['id'].split('/')[-1]: i for i, p in enumerate(passes)}
    _require(len(by_id) == len(passes))
    layers = {layer['layer_id']: layer for layer in scene['active_tree']['layers']
              if layer['draws_content']}
    tile_origins = defaultdict(list)
    for tile in scene['active_tiles']:
        if (tile['has_resource'] and tile['resolution'] == 'HIGH_RESOLUTION'
                and tile['raster_transform'] == {'scale': [1, 1], 'translation': [0, 0]}):
            tile_origins[tuple(tile['content_rect'][:2])].append(tile)
    recording = _Recording()
    count = 0

    def emit(index, depth):
        nonlocal count
        _require(depth <= 16)
        current = passes[index]
        _require(not any(current.get(k) for k in (
            'backdrop_filters', 'filters', 'copy_requests', 'generate_mipmap')))
        recording.op(_Draw.SAVE)
        recording.clip(current['output_rect'])
        # Viz stores quads front-to-back. Skia paints back-to-front.
        for q in reversed(current['quad_list']):
            count += 1
            if q['material'] == _Material.DEBUG_BORDER:  # Chromium's timed diagnostic redraw border.
                raise VizPending('合成器尚未提交关闭诊断边框后的画面。')
            _require(count <= 4096 and q['material'] in (_Material.RENDER_PASS, _Material.SOLID_COLOR, _Material.TILED_CONTENT))
            st = current['shared_quad_state_list'][q['shared_quad_state']['index']]
            _require(st['index'] == q['shared_quad_state']['index'] and not st['sorting_context_id'])
            blend, alpha = st['blend_mode'], st['opacity']
            _require(blend in ('SrcOver', 'DstIn') and 0 <= alpha <= 1)
            recording.op(_Draw.SAVE)
            if 'clip_rect' in st:
                recording.clip(st['clip_rect'])
            _mask(recording, st)
            recording.transform(st['quad_to_target_transform'])
            recording.clip(q['visible_rect'])
            isolate = blend != 'SrcOver' or alpha != 1 or q['material'] == _Material.RENDER_PASS
            if isolate:
                paint = recording.paint((0, 0, 0, alpha), blend)
                recording.op(_Draw.SAVE_LAYER, 'II', 2, paint)  # SaveLayerRec with restore paint
            if q['material'] == _Material.RENDER_PASS:
                child = by_id[q['render_pass_id']['id_ref']]
                _require(child < index and q['rect'] == passes[child]['output_rect']
                         and not any(q.get('mask_texture_size', {}).values()))
                emit(child, depth + 1)
            elif q['material'] == _Material.SOLID_COLOR:
                color = _RGBA.fullmatch(q['color'])
                _require(color)
                rgba = tuple(map(float, color.groups()))
                _require(all(0 <= channel <= 1 for channel in rgba))
                recording.op(_Draw.RECT, 'I4f', recording.paint(rgba), *_edges(q['rect']))
            else:
                ref = _tile_source(layers, tile_origins, current, st, q)
                x, y, raw = source(ref)
                x += st['quad_layer_rect'][0]
                y += st['quad_layer_rect'][1]
                if x or y:
                    recording.op(_Draw.TRANSLATE, '2f', x, y)
                recording.picture(raw)
            if isolate:
                recording.op(_Draw.RESTORE)
            recording.op(_Draw.RESTORE)
        recording.op(_Draw.RESTORE)

    emit(len(passes) - 1, 0)
    return recording.finish(width, height)

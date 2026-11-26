"""Import one complete vector scene from Claude Desktop's own DevTools export."""
import argparse
import base64
from bisect import bisect_right
from collections import defaultdict
import gzip
import json
import math
import os
from pathlib import Path
import struct
import tempfile
import zlib

from .chromium_capture import CaptureError, MAX_TRACE_BYTES, PICTURE, TREE, _drawn_layers, _ref
from .skp_fonts import _Reader, _collection_glyphs
from .viz_picture import _Draw, _Recording
from .wire import Frame, Layer, MAX_LAYERS, MAX_PICTURE, MAX_VIEWPORT, encode_message

_DOCUMENT = 'file:///usr/lib/claude-desktop/resources/app.asar/.vite/renderer/main_window/index.html'
# Skia v110 SkPictureFlat.h: only these fixed-size vector commands are admitted.
# Includes clips, paths, glyph runs and 2D matrices. A bitmap never qualifies.
_VECTOR_SIZES = {3: 28, 4: 60, 12: 24, 14: 12, 21: 24, 22: 56, 28: 4,
                 30: 4, 32: 12, 35: 12, 45: 20, 52: 8, 68: 68}
_CONCAT44 = 68
_MAX_EVENTS = 250_000
_MAX_PICTURES = 512


class ClaudeTraceError(ValueError):
    """This performance trace does not identify a complete supported Claude scene."""


def _require(condition, message):
    if not condition:
        raise ClaudeTraceError(message)


def _numbers(value, count):
    _require(isinstance(value, list) and len(value) == count and
             all(type(n) in (float, int) and -65536 <= n <= 65536 for n in value),
             '图层坐标无效。')
    return value


def _font(reader):
    # Upstream SkFontDescriptor fields. Preserve the font supplied by the
    # exporter and never look up a local or substitute face.
    reader.packed()
    fields = set()
    while (tag := reader.packed()) != 0xff:
        _require(tag not in fields, '字体描述包含重复字段。')
        fields.add(tag)
        if tag in (1, 4, 6):
            size = reader.packed()
            _require(size <= 512, '字体名称超长。')
            reader.take(size)
        elif tag in range(0x10, 0x14):
            reader.take(4)
        elif tag == 0xfa:
            axes = reader.packed()
            _require(axes <= 64, '字体变化轴过多。')
            reader.take(8 * axes)
        elif tag in (0xfc, 0xfd):
            reader.packed()
        else:
            raise ClaudeTraceError('暂未支持这类字体描述。')
    size = reader.packed()
    _require(12 <= size <= 4 << 20, '需要随原厂导出文件提供完整字体。')
    _require(reader.take(size)[:4] in (b'\0\1\0\0', b'OTTO', b'ttcf'),
             '字体文件类型不受支持。')


def _vector_picture(snapshot):
    x, y, width, height = _numbers(snapshot['params']['layer_rect'], 4)
    _require(width > 0 and height > 0, '图层绘制范围为空。')
    encoded = snapshot['skp64']
    _require(isinstance(encoded, str) and len(encoded) <= 4 * ((MAX_PICTURE + 2) // 3),
             '单个绘图记录过大。')
    raw = base64.b64decode(encoded, validate=True)
    _require(len(raw) <= MAX_PICTURE, '单个绘图记录过大。')
    reader = _Reader(raw)
    reader.expect(b'skiapict')
    _require(reader.u32() == 110 and struct.unpack('<4f', reader.take(16)) ==
             (x, y, x + width, y + height), '绘图版本或边界与源图层不符。')
    reader.expect(b'\1daer')
    commands = _Reader(reader.take(reader.u32()))
    depth = 0
    while commands.pos < len(commands.raw):
        word = commands.u32()
        opcode, size = word >> 24, word & 0xffffff
        _require(_VECTOR_SIZES.get(opcode) == size, '此图层使用了尚不支持的绘图指令。')
        payload = commands.take(size - 4)
        if opcode == _Draw.SAVE_LAYER:
            _require(payload == bytes(4), '此独立图层使用了额外的合成效果。')
        depth += (opcode in (_Draw.SAVE, _Draw.SAVE_LAYER)) - (opcode == _Draw.RESTORE)
        _require(0 <= depth <= 64, '图层绘制状态堆栈无效。')
    _require(depth == 0, '图层绘制状态未闭合。')
    reader.expect(b'tcaf')
    _require(reader.u32() == 4 and reader.u32() == 0, '图层含有尚不支持的画笔类型。')
    reader.expect(b'cfpt')
    faces = reader.count()
    _require(faces <= 128, '图层字体过多。')
    for _ in range(faces):
        _font(reader)
    reader.expect(b'yara')
    _collection_glyphs(reader.take(reader.u32()), [None]*faces, 0, vector_only=True)
    reader.expect(b' foe')
    _require(reader.pos == len(raw), '图层末尾含有额外数据。')
    return raw


def _main_tree(events):
    frames = {(f['frame'], f['processId']) for event in events
              if event['name'] == 'TracingStartedInBrowser'
              for f in event['args']['data']['frames'] if f.get('isOutermostMainFrame')
              and f.get('isInPrimaryMainFrame') and f.get('url') == _DOCUMENT}
    _require(len(frames) == 1, '未找到唯一的原版 Claude 主窗口，请选择该窗口的开发者工具导出。')
    frame, pid = frames.pop()
    _require(type(pid) is int and pid > 0 and isinstance(frame, str) and frame,
             '原版窗口渲染进程信息无效。')
    for event in events:
        if event['name'] == 'FrameCommittedInBrowser':
            commit = event.get('args', {}).get('data', {})
            if commit.get('frame') == frame:
                _require(commit.get('processId') == pid and commit.get('url') == _DOCUMENT,
                         '录制期间原版窗口切换了渲染进程或文档。')
    owned = [event for event in events if event.get('pid') == pid]
    tree_ids = {e['args']['data']['layerTreeId'] for e in owned if e['name'] == 'SetLayerTreeId'
                and e.get('args', {}).get('data', {}).get('frame') == frame}
    _require(len(tree_ids) == 1, '未能唯一确认原版窗口的合成树。')
    return owned, tree_ids.pop()


def _source_snapshots(owned, tree_id):
    updates = defaultdict(list)
    snapshots = defaultdict(list)
    for e in owned:
        args = e.get('args', {})
        if e['name'] not in ('UpdateLayer', PICTURE, TREE):
            continue
        _require(type(e['ts']) in (int, float) and math.isfinite(e['ts'])
                 and type(e['tid']) is int, '绘图事件时间或线程标识无效。')
        if e['name'] == 'UpdateLayer' and args.get('layerTreeId') == tree_id and 'dur' in e:
            start, duration, layer = e['ts'], e['dur'], args['layerId']
            _require(type(layer) is int and layer > 0 and type(duration) in (int, float)
                     and math.isfinite(duration) and duration >= 0, '图层更新时间无效。')
            updates[e['tid']].append((start, start + duration, layer))
        elif e['name'] in (PICTURE, TREE) and 'snapshot' in args:
            snapshots[e['tid']].append(e)
    _require(sum(e['name'] == PICTURE for stream in snapshots.values() for e in stream)
             <= _MAX_PICTURES, '录制的绘图记录过多，请缩短录制时间。')
    sources = defaultdict(set)
    for tid, spans in updates.items():
        spans.sort()
        _require(all(b[0] >= a[1] for a, b in zip(spans, spans[1:])),
                 '同一线程的图层更新区间重叠。')
        starts = [span[0] for span in spans]
        for e in snapshots.get(tid, ()):
            if e['name'] == PICTURE:
                i = bisect_right(starts, e['ts']) - 1
                if i >= 0 and e['ts'] < spans[i][1]:
                    encoded = e['args']['snapshot'].get('skp64')
                    _require(isinstance(encoded, str), '缺少 Skia 绘图记录。')
                    sources[encoded].add(spans[i][2])
    return snapshots, sources


def _checkpoint(owned, tree_id):
    streams, sources = _source_snapshots(owned, tree_id)
    selected = None
    for stream in streams.values():
        pending = []
        for e in sorted(stream, key=lambda v: v['ts']):
            if e['name'] == PICTURE:
                pending.append(e['args']['snapshot'])
                continue
            tree = e['args']['snapshot']
            if pending and e['args'].get('layerTreeId') == tree_id:
                layers = _drawn_layers(tree)
                matched = {}
                for snapshot in pending:
                    ids = sources.get(snapshot.get('skp64'), set())
                    if len(ids) == 1:
                        matched[next(iter(ids))] = snapshot
                # Every item in this compositor publication must have identical
                # bytes in one explicit UpdateLayer scope in the owning renderer.
                if (0 < len(matched) == len(layers) == len(pending) <= MAX_LAYERS
                        and set(matched) == {_ref(layer)[0] for layer in layers}
                        and (selected is None or selected[0] < e['ts'])):
                    selected = e['ts'], tree, layers, matched
            pending = []
    _require(selected is not None, '缺少能与原窗口每个活动图层逐一匹配的完整绘图。')
    return selected[1:]


def _draw_layer(recording, layer, snapshot):
    _require(layer['opacity'] == 1 and layer['effect_tree_index'] == 1
             and layer['clip_tree_index'] in (1, 2) and not layer.get('sorting_context_id'),
             '此图层使用了尚不支持的合成效果。')
    w, h = layer['bounds']['width'], layer['bounds']['height']
    _require(type(w) is int and type(h) is int and 0 < w <= 32768 and 0 < h <= 32768,
             '图层尺寸无效。')
    x0, y0, x1, y1, x2, y2, x3, y3 = _numbers(layer['layer_quad'], 8)
    _require(abs(x0 + x2 - x1 - x3) < 0.001 and abs(y0 + y2 - y1 - y3) < 0.001,
             '尚不支持透视变换。')
    clip = _numbers(layer['visible_rect'], 4)
    vx, vy, vw, vh = clip
    _require(0 <= vx < vx + vw <= w and 0 <= vy < vy + vh <= h, '图层裁切范围无效。')
    raw = _vector_picture(snapshot)
    # CONCAT44 is stored column-major by SkPictureRecord::didConcat44.
    recording.op(_Draw.SAVE)
    recording.op(_CONCAT44, '16f', (x1 - x0) / w, (y1 - y0) / w, 0, 0,
                 (x3 - x0) / h, (y3 - y0) / h, 0, 0, 0, 0, 1, 0, x0, y0, 0, 1)
    recording.clip(clip)
    recording.picture(raw)
    recording.op(_Draw.RESTORE)


def import_scene(events, *, activation=None):
    _require(isinstance(events, list) and 0 < len(events) <= _MAX_EVENTS,
             '性能记录为空或事件数量过多。')
    try:
        owned, tree_id = _main_tree(events)
        if activation is None:
            tree, layers, pictures = _checkpoint(owned, tree_id)
        else:
            from .claude_activation import activations
            _require(type(activation) is int and activation >= 0, '原厂活动帧编号无效。')
            scene = next((scene for seq, scene in activations(owned, tree_id) if seq == activation), None)
            _require(scene is not None, '该状态缺少原窗口完整提交与呈现的关联记录。')
            tree, layers, pictures = scene
        viewport = tree['device_viewport_size']
        width, height = viewport['width'], viewport['height']
        _require(all(type(n) is int and 1 <= n <= MAX_VIEWPORT for n in (width, height)),
                 '原版窗口尺寸无效。')
        recording = _Recording()
        for layer in layers:
            _draw_layer(recording, layer, pictures[layer['layer_id']])
        picture = recording.finish(width, height)
        return Frame(1, width, height, (Layer(0, 0, width, height, picture),))
    except ClaudeTraceError:
        raise
    except (AttributeError, CaptureError, KeyError, ValueError, TypeError, IndexError, struct.error, OverflowError) as exc:
        raise ClaudeTraceError('文件缺少完整、受支持的原版绘图记录。') from exc


def _read_events(source):
    _require(source.stat().st_size <= MAX_TRACE_BYTES, '性能记录超过大小上限。')
    with source.open('rb') as f:
        compressed = f.read(2) == b'\x1f\x8b'
        f.seek(0)
        if compressed:
            with gzip.GzipFile(fileobj=f) as archive:
                data = archive.read(MAX_TRACE_BYTES + 1)
        else:
            data = f.read(MAX_TRACE_BYTES + 1)
    _require(len(data) <= MAX_TRACE_BYTES, '解压后的性能记录超过大小上限。')
    parsed = json.loads(data)
    _require(isinstance(parsed, dict), '需要开发者工具的 Performance JSON 导出。')
    events = parsed.get('traceEvents')
    _require(isinstance(events, list) and 0 < len(events) <= _MAX_EVENTS, '事件数量无效。')
    return events


def convert(source, output, *, activation=None):
    if source.resolve() == output.resolve() or (output.exists() and source.samefile(output)):
        raise ClaudeTraceError('请为 CLRP 文件指定独立的输出路径。')
    result = encode_message(import_scene(_read_events(source), activation=activation))
    tmp = None
    try:
        with tempfile.NamedTemporaryFile(dir=output.parent, delete=False) as f:
            tmp = Path(f.name)
            f.write(result)
        os.replace(tmp, output)
    finally:
        if tmp is not None:
            tmp.unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('trace', type=Path)
    parser.add_argument('output', nargs='?', type=Path)
    group = parser.add_mutually_exclusive_group()
    group.add_argument('--activation', type=int, help='native compositor source frame number')
    group.add_argument('--list-activations', action='store_true',
                       help='list explicitly associated native frame IDs in this export')
    args = parser.parse_args()
    try:
        if args.list_activations:
            if args.output is not None:
                parser.error('--list-activations takes no output file')
            from .claude_activation import activations
            owned, tree_id = _main_tree(_read_events(args.trace))
            for sequence, _ in activations(owned, tree_id):
                print(sequence)
        else:
            if args.output is None:
                parser.error('an output CLRP path is required')
            convert(args.trace, args.output, activation=args.activation)
    except (OSError, EOFError, ValueError, TypeError, KeyError, IndexError, AttributeError, CaptureError,
            RecursionError, zlib.error) as exc:
        parser.exit(1, f'导入失败。{exc}\n')


if __name__ == '__main__':
    main()

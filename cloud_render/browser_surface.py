"""Read Chromium's own offline error viewport in a dedicated cloud system browser."""

import asyncio
from collections import defaultdict
import hashlib
from dataclasses import dataclass
import os
from pathlib import Path

from .chromium_capture import (
    CATEGORIES, TREE, PICTURE, CaptureError, _drawn_layers, _exclusive_page, _portable, _ref, _snapshot, _target,
)
from .claude_activation import activations
from .claude_trace import _source_snapshots
from .viz_picture import compose
from .wire import Frame, Layer

OFFLINE_TARGET = 'https://auth.openai.com/'
_CATEGORIES = CATEGORIES + ',cc,benchmark,devtools.timeline,disabled-by-default-devtools.timeline.frame,disabled-by-default-cc.debug.picture'


def _require(test, message):
    if not test:
        raise CaptureError(message)


@dataclass(frozen=True)
class SystemBrowserWindow:
    browser_pid: int
    window_id: int
    target_id: str
    loader_id: str

    @property
    def frame_id(self):
        return self.target_id


async def _window_identity(page, browser, target, expected_pid, executable, *, accepts_target=None):
    _require(_exclusive_page(page) and not page.is_closed(), 'system browser must own exactly one private page')
    processes = (await browser.send('SystemInfo.getProcessInfo'))['processInfo']
    owners = [p['id'] for p in processes if p['type'] == 'browser']
    _require(owners == [expected_pid] and os.path.samefile(f'/proc/{expected_pid}/exe', executable),
             'system browser process changed')
    info = (await target.send('Target.getTargetInfo'))['targetInfo']
    tree = (await target.send('Page.getFrameTree'))['frameTree']
    document = tree['frame']
    attempted = document.get('unreachableUrl')
    allowed = attempted == OFFLINE_TARGET if accepts_target is None else accepts_target(attempted)
    _require(info['type'] == 'page' and allowed and info['url'] == attempted
             and document['url'] == 'chrome-error://chromewebdata/'
             and not tree.get('childFrames') and not document.get('parentId')
             and info['targetId'] == document['id'] and bool(document['loaderId']),
             'capture requires the dedicated public offline document')
    window = await browser.send('Browser.getWindowForTarget', {'targetId': info['targetId']})
    _require(type(window['windowId']) is int and window['bounds']['windowState'] != 'minimized',
             'original browser has no presentable window')
    metrics = (await target.send('Page.getLayoutMetrics'))['cssVisualViewport']
    _require(metrics.get('scale', 1) == metrics.get('zoom', 1) == 1 and
             not metrics['offsetX'] and not metrics['offsetY'], 'unexpected system browser zoom or viewport')
    # Chromium's CSS clientWidth excludes a traditional scrollbar. Viz paints
    # the entire original viewport, including that scrollbar.
    viewport = (await target.send('Runtime.evaluate', {
        'expression': '[window.innerWidth,window.innerHeight,window.devicePixelRatio]', 'returnByValue': True,
    }))['result'].get('value')
    _require(isinstance(viewport, list) and len(viewport) == 3 and viewport[2] == 1 and
             all(type(n) is int and 0 < n <= 8192 for n in viewport[:2]), 'invalid native browser viewport')
    return (SystemBrowserWindow(expected_pid, window['windowId'], info['targetId'], document['loaderId']),
            (viewport[0], viewport[1]))


def _published_pictures(events, renderer, tree_id):
    """Bind anonymous compositor pictures to originals in the renderer's UpdateLayer scopes."""
    owned = [e for e in events if e.get('pid') == renderer]
    streams, scopes = _source_snapshots(owned, tree_id)
    trees = [e for e in owned if e.get('name') == TREE and 'snapshot' in e.get('args', {})]
    _require(trees and {e['args']['layerTreeId'] for e in trees} == {tree_id}
             and len({e['tid'] for e in trees}) == 1, 'ambiguous system browser compositor')
    pictures = {}
    candidates = {}
    pending = []
    final = None
    for event in sorted(streams[trees[0]['tid']], key=lambda e: e['ts']):
        snapshot = event['args']['snapshot']
        if event['name'] == PICTURE:
            pending.append(snapshot)
            continue
        final = snapshot
        for record in pending:
            candidates[record['skp64']] = record
        layers = [_ref(layer) for layer in _drawn_layers(final) if layer.get('pictures')]
        refs = dict(layers)
        _require(len(refs) == len(layers), 'duplicate original browser layer')
        current = {(layer, ref): pictures[layer, ref] for layer, ref in layers if (layer, ref) in pictures}
        seen = set()
        for picture in pending:
            ids = scopes.get(picture['skp64'], set())
            if len(ids) != 1:
                continue
            layer = next(iter(ids))
            if layer not in refs:
                continue
            _require(layer not in seen, 'browser compositor picture is ambiguous')
            key = (layer, refs[layer])
            _require(key not in current or current[key] == picture, 'browser picture changed without a new reference')
            current[key] = picture
            seen.add(layer)
        pictures = current
        pending.clear()
    _require(not pending and final is not None, 'browser compositor publication is incomplete')
    def pictured_layers(tree):
        return [layer for layer in _drawn_layers(tree) if layer.get('pictures')]

    required = {_ref(layer) for layer in pictured_layers(final)}
    if not required.issubset(pictures):
        for seq, (tree, layers, recorded) in activations(
            owned, tree_id, layers_of=pictured_layers, completed_paint=True
        ):
            if seq == final['active_tree']['source_frame_number'] and tree == final:
                pictures = {_ref(layer): recorded[layer['layer_id']] for layer in layers}
    return final, pictures, tuple(candidates.values())


def _embedded_icon(record):
    # Only the standard v110 one-image table at the very end of this
    # original SkPicture is supported. The image always comes from its SKP.
    import base64
    import struct
    from io import BytesIO
    from PIL import Image

    raw = base64.b64decode(record['skp64'], validate=True)
    marker = b'gami' + struct.pack('<II', 1, 0)
    at = raw.rfind(marker)
    if at < 0 or at + 16 > len(raw):
        return None
    n, = struct.unpack_from('<I', raw, at + 12)
    start = at + 16
    if (n > 256 * 1024 or raw[start + n:] != bytes(-n % 4) + b' foe'
            or not raw.startswith(b'\x89PNG\r\n\x1a\n', start)):
        return None
    with Image.open(BytesIO(raw[start:start + n])) as icon:
        if not (0 < icon.width <= 256 and 0 < icon.height <= 256 and icon.format == 'PNG'):
            return None
        return icon.convert('RGBA')


def _comparison_with_original_icon(image, commands, record):
    # DevTools' loadSnapshot decoder can report a 1x1 placeholder even
    # when the original recorded SKP has the complete encoded PNG. Verify
    # that bounded case privately, against the native layer's full RGBA.
    from PIL import Image

    draws = [(i, e['params']) for i, e in enumerate(commands) if e['method'] == 'drawImageRect']
    if len(draws) != 1:
        return None
    index, paint = draws[0]
    if (paint['image']['width'], paint['image']['height']) != (1, 1):
        return None
    icon = _embedded_icon(record)
    if icon is None:
        return None
    rect = dict(left=0, top=0, right=icon.width, bottom=icon.height)
    if paint['src'] != rect or paint['dst'] != rect:
        return None
    # The inspector wraps the loaded original in a full-size clip. Admit
    # only the observed unscaled image over a known opaque white root fill.
    before = commands[:index]
    if [c['method'] for c in before] != ['save', 'clipRect', 'drawRect', 'save', 'concat44']:
        return None
    root_rect = dict(left=0, top=0, right=image.width, bottom=image.height)
    fill = before[2]['params']
    if (before[1]['params']['rect'] != root_rect or fill['rect'] != root_rect
            or fill['paint'].get('color') != '#FFFFFFFF' or fill['paint'].get('blendMode') != 'Src'):
        return None
    matrix = before[4]['params']['matrix44']
    x, y = matrix[12:14]
    if (matrix != [1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, x, y, 0, 1]
            or x != int(x) or y != int(y) or x < 0 or y < 0 or x+icon.width > image.width or y+icon.height > image.height):
        return None
    out = image.copy()
    tile = Image.new('RGBA', icon.size, (255, 255, 255, 255))
    tile.alpha_composite(icon)
    out.paste(tile, (int(x), int(y)))
    return hashlib.sha256(out.tobytes()).digest()


async def _raster_digest(target, command, arguments, record=None):
    # Both comparison rasters and diagnostic commands stay on the source.
    # The selected original SKP always retains its unmodified image assets.
    import base64
    from io import BytesIO
    from PIL import Image

    handle = (await target.send(command, arguments))['snapshotId']
    try:
        image = (await target.send('LayerTree.replaySnapshot', {'snapshotId': handle}))['dataURL']
        prefix = 'data:image/png;base64,'
        _require(image.startswith(prefix) and len(image) <= 24 << 20, 'invalid original browser layer readback')
        with Image.open(BytesIO(base64.b64decode(image[len(prefix):], validate=True))) as result:
            _require(result.width * result.height <= 4_194_304, 'original browser layer exceeds comparison budget')
            rgba = result.convert('RGBA')
        fingerprint = hashlib.sha256(rgba.tobytes()).digest()
        if record is None:
            return {fingerprint}
        log = (await target.send('LayerTree.snapshotCommandLog', {'snapshotId': handle}))['commandLog']
        corrected = _comparison_with_original_icon(rgba, log, record)
        return {fingerprint, corrected} if corrected is not None else {fingerprint}
    finally:
        await target.send('LayerTree.releaseSnapshot', {'snapshotId': handle})


async def _identify_idle_layers(target, tree, pictures, candidates):
    # A still window can publish compositor SkPictures without any main-thread
    # UpdateLayer in the trace. Compare their entire Chromium-native rendering
    # with the snapshots of the actual CDP layer IDs in this private target.
    missing = [layer for layer in _drawn_layers(tree) if layer.get('pictures') and _ref(layer) not in pictures]
    if not missing:
        return
    _require(0 < len(candidates) <= 32 and len(missing) <= 16, 'unresolved browser layers exceed the comparison budget')
    by_size = defaultdict(list)
    for record in candidates:
        x, y, w, h = record['params']['layer_rect']
        if x == y == 0 and 0 < w * h <= 4_194_304:
            by_size[w, h].append(record)
    compared = {}
    for layer in missing:
        width, height = layer['bounds']['width'], layer['bounds']['height']
        possible = by_size[width, height]
        _require(possible, 'no original drawing matches this browser layer')
        actual = await _raster_digest(target, 'LayerTree.makeSnapshot', {'layerId': str(layer['layer_id'])})
        matches = []
        for record in possible:
            raw = record['skp64']
            if raw not in compared:
                compared[raw] = await _raster_digest(target, 'LayerTree.loadSnapshot',
                                                     {'tiles': [{'x': 0, 'y': 0, 'picture': raw}]}, record)
            if compared[raw] & actual:
                matches.append(record)
        _require(len(matches) == 1, 'idle browser drawing is missing or ambiguous')
        pictures[_ref(layer)] = matches[0]


async def _extract(target, events, window, size, sequence):
    renderer, tree_id = _target(events, window.target_id)
    tree, pictures, candidates = _published_pictures(events, renderer, tree_id)
    await _identify_idle_layers(target, tree, pictures, candidates)
    width, height = size
    _require(tree['device_viewport_size'] == {'width': width, 'height': height}, 'browser viewport changed')
    converted = {}

    def source(ref):
        _require(ref in pictures, 'current browser tile has no original drawing')
        if ref not in converted:
            x, y, _, _, portable = _portable(pictures[ref])
            converted[ref] = (x, y, portable)
        return converted[ref]

    recording = compose(tree, width, height, source)
    _require(converted, 'browser viewport has no verified original picture')
    return Frame(sequence, width, height, (Layer(0, 0, width, height, recording),))


async def capture_offline_viewport(page, sequence, *, browser_pid, executable):
    """Return (native window identity, original drawing) for the cloud-owned, sandboxed Chromium Page.

    The caller owns the browser's private DevTools pipe and its isolated profile. Only
    the built-in network-error document for the fixed, query-free target is supported.
    """
    return await _capture_owned_viewport(page, sequence, browser_pid, executable)


async def _capture_owned_viewport(page, sequence, browser_pid, executable, *, accepts_target=None, snapshot=None):
    _require(type(browser_pid) is int and browser_pid > 0 and Path(executable).is_absolute(),
             'expected a known system-browser process and executable')
    browser = await page.context.browser.new_browser_cdp_session()
    try:
        target = await page.context.new_cdp_session(page)
        try:
            async with asyncio.timeout(15):
                async def identity():
                    return await _window_identity(page, browser, target, browser_pid, executable, accepts_target=accepts_target)

                start = await identity()
                await target.send('LayerTree.enable')
                await target.send('DOM.enable')
                await target.send('Overlay.enable')
                for attempt in range(3):
                    events = (await _snapshot(target, categories=_CATEGORIES) if snapshot is None
                              else await snapshot(target, start[0]))
                    _require(start == await identity(),
                             'browser navigated, resized or switched windows during paint')
                    try:
                        frame = await _extract(target, events, *start, sequence)
                        _require(start == await identity(),
                                 'browser left its original window before publication')
                        return start[0], frame
                    except (CaptureError, ValueError):
                        if attempt == 2:
                            raise
        finally:
            await target.detach()
    finally:
        await browser.detach()

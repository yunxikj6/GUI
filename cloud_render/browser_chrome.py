"""Recover a Browser-process UI layer from a trace of an owned, isolated Chromium window."""

from dataclasses import dataclass

from .chromium_capture import CaptureError, PICTURE, TREE, _drawn_layers, _portable, _ref
from .claude_activation import _cover, _index, _time
from .wire import Frame, Layer


ROLES = frozenset(('DesktopNativeWidgetAura - content window', 'LocationBarView', 'TabStripControlButton',
                   'TabStripComboButton', 'ImageView'))


@dataclass(frozen=True)
class NativeChromeLayer:
    name: str
    layer_id: int
    painted_frame: int
    active_frame: int
    x: int
    y: int
    frame: Frame


def _require(value, reason):
    if not value:
        raise CaptureError(reason)


def _span(event):
    start = _time(event['ts'])
    return start, start + _time(event['dur'])


def _recordings(events, thread, tree_id):
    updates = {}
    rounds = {}
    draw = []
    for event in events:
        if event['tid'] != thread or event['ph'] != 'X':
            continue
        name = event['name']
        args = event['args']
        if name == 'LayerTreeHost::DoUpdateLayers':
            number = args['source_frame_number']
            _require(type(number) is int and number >= 0 and number not in rounds, 'ambiguous Browser paint frame')
            rounds[number] = _span(event)
        elif name == 'DrawLayers.FrameViewerTracing':
            span = _span(event)
            draw.append((*span, span))
        elif name == 'UpdateLayer' and args.get('layerTreeId') == tree_id:
            layer = args['layerId']
            _require(type(layer) is int and layer > 0, 'invalid Browser layer number')
            updates.setdefault(layer, []).append(_span(event))

    rounds_index = _index([(*span, number) for number, span in rounds.items()])
    scoped = []
    seen = set()
    for layer, spans in updates.items():
        for start, end in spans:
            number = _cover(rounds_index, start, end)
            if number is not None:
                _require((number, layer) not in seen, 'duplicate Browser layer update')
                seen.add((number, layer))
                scoped.append((start, end, (number, layer)))
    index = _index(scoped)
    pictures = {}
    for event in events:
        if event['tid'] == thread and event['name'] == PICTURE and 'snapshot' in event['args']:
            when = _time(event['ts'])
            scope = _cover(index, when, when)
            if scope is not None:
                pictures.setdefault(scope, []).append(event['args']['snapshot'])
    return rounds, pictures, _index(draw)


def extract_native_chrome_layer(events, *, browser_pid, name, sequence):
    """Return one original SkPicture at its active placement, or refuse an incomplete trace.

    The caller owns the source browser, its private diagnostic pipe and the single
    displayed window. The explicit PID comes from that live process. This parser
    has no authority to choose a browser, to attach to one, or to approve input.
    Only use a recording of a non-sensitive window prepared for this purpose.
    """
    _require(name in ROLES and type(browser_pid) is int and browser_pid > 0, 'unknown native chrome source')
    _require(isinstance(events, list) and 0 < len(events) <= 250_000, 'invalid Browser recording size')
    try:
        processes = {e['pid'] for e in events if e['name'] == 'process_name' and e['args'].get('name') == 'Browser'}
        _require(processes == {browser_pid}, 'recording does not identify the owned Browser process')
        owned = [e for e in events if e['pid'] == browser_pid]
        trees = sorted((e for e in owned if e['name'] == TREE and 'snapshot' in e['args']), key=lambda e: e['ts'])
        _require(0 < len(trees) <= 1024, 'missing or excessive Browser active trees')
        ids = {(e['tid'], e['args']['layerTreeId']) for e in trees}
        _require(len(ids) == 1, 'ambiguous native Browser window or compositor')
        thread, tree_id = ids.pop()
        rounds, pictures, draws = _recordings(owned, thread, tree_id)
        previous = identity = original = painted_frame = None
        presented = set()
        for tree in trees:
            snapshot = tree['args']['snapshot']
            active = snapshot['active_tree']['source_frame_number']
            draw_time = _time(tree['ts'])
            draw = _cover(draws, draw_time, draw_time)
            _require(type(active) is int and active >= 0 and draw is not None and active not in presented,
                     'Browser tree was not submitted by its native compositor')
            presented.add(active)
            # A later paint may have started by the time this tree is serialized.
            # Only use the matching source frame, completed before this draw.
            same_generation = previous is not None and active == previous + 1
            previous = active
            matching = [layer for layer in _drawn_layers(snapshot) if layer.get('layer_name') == name]
            if (len(matching) != 1 or not matching[0].get('pictures') or active not in rounds
                    or rounds[active][1] > draw[0]):
                identity = original = None
                continue
            layer = matching[0]
            layer_id, ref = _ref(layer)
            bounds = layer['bounds']
            key = (layer_id, ref, bounds['width'], bounds['height'])
            records = pictures.get((active, layer_id), ())
            if len(records) == 1:
                original = records[0]
                painted_frame = active
            elif records or key != identity or not same_generation:
                original = None
            identity = key
        _require(original is not None, 'native Browser layer has no continuous original paint record')
        x, y = layer['layer_quad'][:2]
        width, height = bounds['width'], bounds['height']
        transform = [1, 0, 0, x, 0, 1, 0, y, 0, 0, 1, 0, 0, 0, 0, 1]
        identity_at_origin = (x, y) == (0, 0) and 'screen_space_transform' not in layer
        _require(type(x) is int and type(y) is int and layer['opacity'] == 1 and not layer.get('sorting_context_id') and
                 layer['effect_tree_index'] == layer['clip_tree_index'] == 1 and
                 (identity_at_origin or layer.get('screen_space_transform') == transform) and
                 layer['layer_quad'] == [x, y, x+width, y, x+width, y+height, x, y+height] and
                 layer.get('visible_rect') == [0, 0, width, height] and
                 original['params']['layer_rect'] == [0, 0, width, height], 'unsupported native Browser layer geometry')
        _, _, _, _, skp = _portable(original)
        frame = Frame(sequence, width, height, (Layer(0, 0, width, height, skp),))
        return NativeChromeLayer(name, layer_id, painted_frame, active, x, y, frame)
    except (KeyError, IndexError, TypeError, ValueError) as exc:
        raise CaptureError('malformed Browser paint recording') from exc

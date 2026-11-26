"""Resolve the pinned Linux application's own non-client titlebar vector recordings."""

import base64
import hashlib
import struct

from cloud_render.wire import Layer

ORIGINAL_SHA256 = 'b36d94aff6ec46074f9d4ac5f898d1cc449396b661663487fcf2d11e41c502a7'
# Unmodified, normal, light caption controls in ChatGPT 26.1120.50957, DPR 1.
# These are digests only. All command bytes must come from this running program.
_CAPTIONS = (
    (
        'ChromeNodeCaptionButtonContainer',
        1026,
        64,
        '093be95b3e6a1641d2531edfcc200f1668d5b63a8159b360bea1043bbad25e96',
    ),
    ('ChromeNodeCaptionButton', 1026, 32, 'b41a1710394ee45dfd3fb353a9edf452af4b888385a301120a3b051370ca2f4d'),
    ('ChromeNodeCaptionButton', 1058, 32, '0e3423ff6bdc1942e5f9e5d28170a00ef299ec4e885ccbbec975c02eb64adca4'),
)


class WitnessError(ValueError):
    """The source did not establish the identity, geometry or paint of this window."""


def require(condition, reason):
    if not condition:
        raise WitnessError(reason)


def caption_layers(events, *, browser_pid, geometry):
    """Return only three identified native browser records, in painter order.

    Callers own exactly one app target, verified executable and Wayland toplevel.
    The two buttons use DRAW_PATH in the original SKPs. No static assets ship.
    """
    require(geometry == (1090, 760, 1, 1026, 36, 'light'), 'unsupported native titlebar geometry')
    browser = [e for e in events if e.get('pid') == browser_pid]
    require(
        any(e['name'] == 'process_name' and e['args'].get('name') == 'Browser' for e in browser),
        'wrong trace process',
    )
    trees = [
        e
        for e in browser
        if e['name'] == 'LayerTreeHostImpl:snapshot' and isinstance(e['args'].get('snapshot'), dict)
    ]
    require(trees and len({e['args']['layerTreeId'] for e in trees}) == 1, 'ambiguous native widget')
    tree = max(trees, key=lambda e: e['ts'])
    shown = tree['args']['snapshot']['active_tree']['layers']
    web = [
        l
        for l in shown
        if l.get('layer_name') == 'RenderWidgetHostViewAura'
        and l.get('draws_content')
        and l.get('layer_quad') == [0, 0, 1090, 0, 1090, 760, 0, 760]
    ]
    require(
        len(web) == 1 and 'SurfaceRange' in web[0].get('surface_range', ''),
        'missing original browser content surface',
    )
    names = {caption[0] for caption in _CAPTIONS}
    native = [
        layer
        for layer in shown
        if layer.get('layer_name') in names and layer.get('visible_rect', [0, 0, 0, 0])[2:] != [0, 0]
    ]
    require(len(native) == len(_CAPTIONS), 'unrecognized visible caption controls')
    drawings = [
        e
        for e in browser
        if e['name'] == 'cc::DisplayItemList:snapshot'
        and isinstance(e['args'].get('snapshot'), dict)
        and e['ts'] <= tree['ts']
    ]
    result = []
    for layer, (name, x, width, digest) in zip(native, _CAPTIONS):
        rect = [0, 0, width, 36]
        require(
            layer['layer_name'] == name
            and layer.get('visible_rect') == rect
            and layer.get('bounds') == {'width': width, 'height': 36}
            and layer.get('opacity') == 1
            and layer.get('layer_quad') == [x, 0, x + width, 0, x + width, 36, x, 36]
            and layer.get('screen_space_transform') == [1, 0, 0, x, 0, 1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1]
            and layer.get('effect_tree_index') == 1,
            'unsupported native control composition',
        )
        refs = layer.get('pictures', [])
        require(len(refs) == 1, 'missing unique caption picture')
        candidates = [
            e
            for e in drawings
            if e['args']['id'] == refs[0]['id_ref'] and e['args']['layer_id'] == layer['layer_id']
        ]
        require(candidates, 'unresolved native picture reference')
        snapshot = max(candidates, key=lambda e: e['ts'])['args']['snapshot']
        encoded = snapshot.get('skp64', '')
        require(
            snapshot.get('params', {}).get('layer_rect') == rect and len(encoded) < 1400,
            'unexpected native picture bounds or length',
        )
        raw = base64.b64decode(encoded, validate=True)
        require(
            hashlib.sha256(raw).hexdigest() == digest
            and raw.startswith(b'skiapict')
            and struct.unpack_from('<4f', raw, 12) == tuple(rect),
            'unknown caption paint or state',
        )
        result.append(Layer(x, 0, width, 36, raw))
    return tuple(result)

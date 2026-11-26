import base64
from copy import deepcopy
import gzip
import json
from pathlib import Path
import struct
import tempfile
import unittest

from cloud_render import claude_trace as importer
from cloud_render.viz_picture import _Draw, _Recording
from cloud_render.wire import encode_message
from experiments.inspect_native_frame import load_frame


def vector(width, height, color):
    canvas = _Recording()
    canvas.op(_Draw.RECT, 'I4f', canvas.paint(color), 0, 0, width, height)
    return canvas.finish(width, height)


def sample():
    """Two independently scoped vector pictures from a miniature trace."""
    width, height = 80, 50
    raw = [vector(width, height, (1, 1, 1, 1)), vector(8, 8, (0, 0, 1, 1))]
    layers = []
    events = [
        {'name': 'TracingStartedInBrowser', 'pid': 2, 'args': {'data': {'frames': [{
            'frame': 'frame', 'processId': 7, 'isOutermostMainFrame': True,
            'isInPrimaryMainFrame': True, 'url': importer._DOCUMENT}]}}},
        {'name': 'SetLayerTreeId', 'pid': 7, 'args': {'data': {'frame': 'frame', 'layerTreeId': 6}}},
    ]
    for i, (w, h) in enumerate(((width, height), (8, 8))):
        layer = i + 5
        quad = [0, 0, w, 0, w, h, 0, h] if i == 0 else [18, 4, 18, 12, 10, 12, 10, 4]
        layers.append({'layer_id': layer, 'pictures': [{'id_ref': str(layer)}],
                       'draws_content': True, 'opacity': 1, 'effect_tree_index': 1,
                       'clip_tree_index': 2, 'visible_rect': [0, 0, w, h],
                       'bounds': {'width': w, 'height': h}, 'layer_quad': quad})
        snapshot = {'params': {'layer_rect': [0, 0, w, h]},
                    'skp64': base64.b64encode(raw[i]).decode()}
        stamp = 20 * i + 1
        events.extend([
            {'name': 'UpdateLayer', 'pid': 7, 'tid': 1, 'ts': stamp, 'dur': 3,
             'args': {'layerId': layer, 'layerTreeId': 6}},
            {'name': importer.PICTURE, 'pid': 7, 'tid': 1, 'ts': stamp + 1,
             'args': {'snapshot': snapshot}},
            {'name': importer.PICTURE, 'pid': 7, 'tid': 3, 'ts': 60+i,
             'args': {'snapshot': snapshot}},
        ])
    tree = {'active_tree': {'layers': layers}, 'device_viewport_size': {'width': width, 'height': height}}
    events.append({'name': importer.TREE, 'pid': 7, 'tid': 3, 'ts': 70,
                   'args': {'layerTreeId': 6, 'snapshot': tree}})
    return events, raw


class ClaudeTraceTests(unittest.TestCase):
    def test_complete_affine_replay_uses_only_the_target_process(self):
        events, raw = sample()
        foreign = deepcopy(events)
        for e in foreign:
            e['pid'] = 11
        foreign = [e for e in foreign if e['name'] not in ('TracingStartedInBrowser', 'SetLayerTreeId')]
        frame = importer.import_scene(events + foreign)
        self.assertEqual((frame.width, frame.height), (80, 50))
        picture, = frame.layers
        for child in raw:
            self.assertIn(child, picture.skp)
        rotation = struct.pack('<16f', 0, 1, 0, 0, -1, 0, 0, 0, 0, 0, 1, 0, 18, 4, 0, 1)
        self.assertIn(rotation, picture.skp)

    def test_partial_ambiguous_foreign_and_navigated_sources_are_rejected(self):
        baseline, _ = sample()
        cases = []
        missing = [e for e in baseline if not (e.get('tid') == 1 and e.get('name') == importer.PICTURE)]
        cases.append(missing)
        ambiguous = deepcopy(baseline)
        ambiguous[6]['args']['snapshot']['skp64'] = ambiguous[3]['args']['snapshot']['skp64']
        cases.append(ambiguous)
        foreign = deepcopy(baseline)
        foreign[-1]['pid'] = 17
        cases.append(foreign)
        changed = deepcopy(baseline)
        changed[0]['args']['data']['frames'][0]['url'] = 'devtools://devtools/bundled/inspector.html'
        cases.append(changed)
        cases.append(baseline + [{'name': 'FrameCommittedInBrowser', 'pid': 2,
                                 'args': {'data': {'frame': 'frame', 'processId': 9, 'url': importer._DOCUMENT}}}])
        for events in cases:
            with self.subTest(events=len(events)), self.assertRaises(importer.ClaudeTraceError):
                importer.import_scene(events)

    def test_perspective_image_and_missing_font_data_are_rejected(self):
        events, sources = sample()
        events[-1]['args']['snapshot']['active_tree']['layers'][0]['layer_quad'][4] += 2
        with self.assertRaises(importer.ClaudeTraceError):
            importer.import_scene(events)

        with self.assertRaises(importer.ClaudeTraceError):
            importer._font(importer._Reader(b'\1\xfe\xff\0\0'))
        fake = bytearray(sources[0])
        fake[40] = 73  # Skia DRAW_IMAGE_RECT2
        snap = {'params': {'layer_rect': [0, 0, 80, 50]}, 'skp64': base64.b64encode(fake).decode()}
        with self.assertRaises(importer.ClaudeTraceError):
            importer._vector_picture(snap)

    def test_plain_and_gzip_share_output_and_failed_import_is_atomic(self):
        events, _ = sample()
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            output = folder / 'out.clrp'
            data = json.dumps({'traceEvents': events}).encode()
            for path, payload in [(folder/'trace.json', data), (folder/'trace.gz', gzip.compress(data))]:
                path.write_bytes(payload)
                importer.convert(path, output)
                self.assertEqual(output.read_bytes(), encode_message(load_frame(output)))
            saved = output.read_bytes()
            corrupted = folder / 'invalid.json'
            corrupted.write_text('{"traceEvents":[]}')
            with self.assertRaises(importer.ClaudeTraceError):
                importer.convert(corrupted, output)
            self.assertEqual(output.read_bytes(), saved)
            with self.assertRaises(importer.ClaudeTraceError):
                importer.convert(output, output)


if __name__ == '__main__':
    unittest.main()

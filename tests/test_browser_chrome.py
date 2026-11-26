from copy import deepcopy
import unittest
from unittest.mock import patch

from cloud_render.browser_chrome import extract_native_chrome_layer
from cloud_render.chromium_capture import CaptureError, PICTURE, TREE


def event(name, ts, args, **extras):
    return dict(pid=5, tid=5, name=name, ts=ts, args=args, **extras)


def capture(number, tick, *, painted=True, ref='native-A'):
    layer = {'layer_id': 8, 'layer_name': 'LocationBarView', 'draws_content': 1, 'opacity': 1,
             'bounds': {'width': 48, 'height': 24}, 'visible_rect': [0, 0, 48, 24],
             'clip_tree_index': 1, 'effect_tree_index': 1,
             'screen_space_transform': [1, 0, 0, 4, 0, 1, 0, 6, 0, 0, 1, 0, 0, 0, 0, 1],
             'layer_quad': [4, 6, 52, 6, 52, 30, 4, 30], 'pictures': [{'id_ref': ref}]}
    records = [event('LayerTreeHost::DoUpdateLayers', tick, {'source_frame_number': number}, ph='X', dur=12),
               event('UpdateLayer', tick+1, {'layerTreeId': 1, 'layerId': 8}, ph='X', dur=5),
               event('DrawLayers.FrameViewerTracing', tick+15, {}, ph='X', dur=4),
               event(TREE, tick+16, {'layerTreeId': 1, 'snapshot': {'active_tree':
                      {'source_frame_number': number, 'layers': [layer]}}}, ph='I')]
    if painted:
        records.append(event(PICTURE, tick+2, {'snapshot': {'params': {'layer_rect': [0, 0, 48, 24]},
                                                            'skp64': 'test-only'}}, ph='I'))
    return records


class OriginalBrowserChrome(unittest.TestCase):
    def run_layer(self, records):
        source = [event('process_name', 0, {'name': 'Browser'}, ph='M'), *records]
        with patch('cloud_render.browser_chrome._portable', return_value=(0, 0, 48, 24, b'skiapicttest')):
            return extract_native_chrome_layer(source, browser_pid=5, name='LocationBarView', sequence=7)

    def test_same_layer_ref_survives_only_consecutive_confirmed_source_frames(self):
        result = self.run_layer(capture(12, 10) + capture(13, 45, painted=False))
        self.assertEqual((result.layer_id, result.painted_frame, result.active_frame), (8, 12, 13))
        self.assertEqual((result.x, result.y, result.frame.width, result.frame.height), (4, 6, 48, 24))
        for late in (capture(14, 45, painted=False), capture(13, 45, painted=False, ref='other')):
            with self.subTest(late=late), self.assertRaises(CaptureError):
                self.run_layer(capture(12, 10) + late)

    def test_drawing_requires_the_owned_process_paint_scope_and_active_native_tree(self):
        original = capture(12, 10)
        for index, field, value in ((4, 'pid', 9), (4, 'ts', 18), (4, 'tid', 119), (3, 'ts', 31)):
            altered = deepcopy(original)
            altered[index][field] = value
            with self.subTest(field=field, value=value), self.assertRaises(CaptureError):
                self.run_layer(altered)
        with self.assertRaises(CaptureError):
            self.run_layer(original + [deepcopy(original[4])])
        with self.assertRaises(CaptureError):
            self.run_layer(original + [dict(original[3], tid=13)])

    def test_ambiguous_draws_clips_transforms_and_unfinished_paint(self):
        original = capture(12, 10)
        for extra in (dict(original[1], ts=18, dur=1), deepcopy(original[3])):
            with self.subTest(name=extra['name']), self.assertRaises(CaptureError):
                self.run_layer(original + [extra])
        for field, value in (('clip_tree_index', 7), ('visible_rect', [1, 1, 46, 22]),
                             ('screen_space_transform', [1] * 16)):
            altered = deepcopy(original)
            altered[3]['args']['snapshot']['active_tree']['layers'][0][field] = value
            with self.subTest(field=field), self.assertRaises(CaptureError):
                self.run_layer(altered)
        altered = deepcopy(original)
        altered[0]['dur'] = 16
        with self.assertRaises(CaptureError):
            self.run_layer(altered)

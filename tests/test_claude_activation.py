import base64
from copy import deepcopy
import unittest

from cloud_render import claude_trace
from cloud_render.claude_activation import activations
from tests.test_claude_trace import sample, vector


def captured_states():
    events, raw = sample()
    tree = events[-1]['args']['snapshot']
    pictures = [e['args']['snapshot'] for e in events if e['name'] == claude_trace.PICTURE and e['tid'] == 1]
    events = events[:2]

    def add(n, timestamp, thread, phase, args=None, **more):
        events.append(dict(pid=7, name=n, ts=timestamp, tid=thread, ph=phase, args=args or {}, **more))

    replacement = vector(8, 8, (0, 1, 0, 1))
    for index in range(3):
        origin = 100 * index
        # The Chromium reporter reuses local id2 values across frames.
        track = {'local': '0xb'}
        native = index + 90
        report = {'layer_tree_host_id': 6, 'state': 'STATE_PRESENTED_ALL', 'frame_sequence': native}
        add('PipelineReporter', origin, 3, 'b', {'frame_reporter': report}, id2=track)
        add('Commit', origin+10, 1, 'X', {'layerTreeId': 6, 'frameSeqId': native}, dur=15)
        state = deepcopy(tree)
        state['active_tree']['source_frame_number'] = native
        for j, snap in enumerate(pictures):
            layer = state['active_tree']['layers'][j]
            layer_id = layer['layer_id']
            t = 12 + j*6 + origin
            add('UpdateLayer', t, 1, 'X', {'layerTreeId': 6, 'layerId': layer_id}, dur=4)
            if not index or j == 1:
                if index == 1:
                    snap = dict(snap, skp64=base64.b64encode(replacement).decode())
                    layer['pictures'] = [{'id_ref': 'new'}]
                add(claude_trace.PICTURE, t+1, 1, 'I', {'snapshot': snap})
        add('Activation', origin+30, 3, 'b', id2=track)
        add('ActivateLayerTree', origin+31, 3, 'I', {'layerTreeId': 6, 'frameId': native})
        add('Activation', origin+33, 3, 'e', id2=track)
        add(claude_trace.TREE, origin+34, 3, 'I', {'layerTreeId': 6, 'snapshot': state})
        add('PipelineReporter', origin+40, 3, 'e', id2=track)
    return events, raw, replacement


class ClaudeActivationTests(unittest.TestCase):
    def test_reused_pipeline_ids_and_original_changed_button(self):
        events, originals, replacement = captured_states()
        self.assertEqual([90, 91, 92], [i for i, _ in activations(*claude_trace._main_tree(events))])
        a, b, c = [claude_trace.import_scene(events, activation=n).layers[0].skp for n in (90, 91, 92)]
        self.assertIn(originals[0], b)
        self.assertIn(originals[1], a)
        self.assertIn(replacement, b)
        self.assertNotIn(originals[1], b)
        self.assertEqual(a, c)

    def test_empty_save_layer_keeps_vector_contents_and_rejects_effect_flags(self):
        from cloud_render.viz_picture import _Draw, _Recording
        rec = _Recording()
        rec.op(_Draw.SAVE_LAYER, 'I', 0)
        rec.op(_Draw.RECT, 'I4f', rec.paint((0, 0, 1, 1)), 0, 0, 8, 8)
        rec.op(_Draw.RESTORE)
        raw = rec.finish(8, 8)
        description = {'params': {'layer_rect': [0, 0, 8, 8]}, 'skp64': base64.b64encode(raw).decode()}
        self.assertEqual(raw, claude_trace._vector_picture(description))
        changed = bytearray(raw)
        changed[41] = 32
        description['skp64'] = base64.b64encode(changed).decode()
        with self.assertRaises(claude_trace.ClaudeTraceError):
            claude_trace._vector_picture(description)

    def test_missing_or_ambiguous_source_state_cannot_be_inherited(self):
        events, _, _ = captured_states()
        options = []
        for name, at in [('Commit',110), ('Activation',130), ('PipelineReporter',100),
                         ('UpdateLayer',112), (claude_trace.PICTURE,119)]:
            options.append([e for e in events if not (e['name'] == name and e.get('ts') == at)])
        dup = deepcopy(events)
        dup.append(next(e for e in dup if e['name'] == claude_trace.PICTURE and e.get('ts') == 119))
        options.append(dup)
        bad_ref = deepcopy(events)
        tree = next(e for e in bad_ref if e['name'] == claude_trace.TREE and e['ts'] == 134)
        tree['args']['snapshot']['active_tree']['layers'][0]['pictures'][0]['id_ref'] = 'unwitnessed'
        options.append(bad_ref)
        for case in options:
            for n in (91, 92):
                with self.subTest(n=n), self.assertRaises(claude_trace.ClaudeTraceError):
                    claude_trace.import_scene(case, activation=n)

"""Correlate an original main-thread paint with the compositor that presented it."""
from bisect import bisect_left, bisect_right
from collections import defaultdict
import math

from .wire import MAX_LAYERS
from .chromium_capture import _drawn_layers, _ref, PICTURE, TREE


def _index(items):
    """Index disjoint time spans for one thread or local async track."""
    rows = sorted(items, key=lambda r: r[0])
    if any(stop < start or (i and start < rows[i-1][1])
           for i, (start, stop, _) in enumerate(rows)):
        raise ValueError('overlapping original paint intervals')
    return [r[0] for r in rows], rows


def _cover(index, start, stop):
    starts, rows = index
    i = bisect_right(starts, start) - 1
    if i >= 0 and start < rows[i][1] and stop <= rows[i][1]:
        return rows[i][2]
    return None


def _time(value):
    if type(value) not in (int, float) or not math.isfinite(value):
        raise ValueError('invalid drawing timestamp')
    return value


def _root_spans(events):
    opened = {}
    closed = defaultdict(list)
    for e in events:
        track = e.get('id2', {}).get('local')
        if not isinstance(track, str):
            continue
        key = e['name'], e['tid'], track
        if e['ph'] == 'b':
            if key in opened:
                raise ValueError('ambiguous compositor pipeline')
            opened[key] = e
        elif e['ph'] == 'e':
            start = opened.pop(key, None)
            if start is not None:
                closed[key].append((start['ts'], e['ts'], start))
    reports = {k[1:]: _index(spans) for k, spans in closed.items() if k[0] == 'PipelineReporter'}
    activations = defaultdict(list)
    for (name, tid, track), spans in closed.items():
        if name != 'Activation' or (tid, track) not in reports:
            continue
        for start, stop, _ in spans:
            report = _cover(reports[tid, track], start, stop)
            if report is not None:
                activations[tid].append((start, stop, (report, start)))
    return {tid: _index(rows) for tid, rows in activations.items()}


def _scoped_pictures(events, tree_id):
    commits = defaultdict(list)
    by_sequence = defaultdict(list)
    updates = defaultdict(list)
    for e in events:
        args = e.get('args', {})
        if not (e['name'] in ('Commit', 'UpdateLayer') and e['ph'] == 'X'
                and args.get('layerTreeId') == tree_id):
            continue
        start = e['ts']
        stop = start + _time(e['dur'])
        if stop < start:
            raise ValueError('negative drawing interval')
        if e['name'] == 'Commit':
            by_layer = {}
            rec = (start, stop, by_layer)
            by_sequence[args['frameSeqId']].append(rec)
            commits[e['tid']].append(rec)
        else:
            updates[e['tid']].append((start, stop, args['layerId']))
    paint_spans = {}
    for tid in commits.keys() & updates.keys():
        commit_spans = _index(commits[tid])
        rows = []
        for start, stop, layer_id in updates[tid]:
            layers = _cover(commit_spans, start, stop)
            if layers is None:
                continue
            if layer_id in layers:
                raise ValueError('duplicate layer in one native commit')
            bucket = layers[layer_id] = []
            rows.append((start, stop, bucket))
        paint_spans[tid] = _index(rows)
    for e in events:
        if e['name'] == PICTURE and e['tid'] in paint_spans and 'snapshot' in e.get('args', {}):
            bucket = _cover(paint_spans[e['tid']], e['ts'], e['ts'])
            if bucket is not None:
                bucket.append(e['args']['snapshot'])
    return by_sequence


def activations(owned, tree_id):
    """Yield one actual active scene per proven presentation, in source order."""
    names = {'Activation', 'PipelineReporter', 'Commit', 'UpdateLayer', 'ActivateLayerTree', PICTURE, TREE}
    events = sorted((e for e in owned if e['name'] in names), key=lambda e: _time(e['ts']))
    if sum(e['name'] == PICTURE and 'snapshot' in e.get('args', {}) for e in events) > 512:
        raise ValueError('too many original drawing publications')
    commits = _scoped_pictures(events, tree_id)
    if any(len(v) > 32 for v in commits.values()):
        raise ValueError('ambiguous reused compositor frame sequence')
    pipeline = _root_spans(e for e in events if e['name'] in ('PipelineReporter', 'Activation'))
    actions = [e for e in events if e['name'] == 'ActivateLayerTree'
               and e.get('args', {}).get('layerTreeId') == tree_id]
    if len(actions) > 1024 or len({e['tid'] for e in actions}) > 1:
        raise ValueError('unsupported compositor activation tracks')
    trees = [e for e in events if e['name'] == TREE and 'snapshot' in e.get('args', {})
             and e['args'].get('layerTreeId') == tree_id and actions and e['tid'] == actions[0]['tid']]
    ticks = [e['ts'] for e in trees]
    previous = {}
    last = -1
    for i, action in enumerate(actions):
        sequence = action['args']['frameId']
        if type(sequence) is not int or sequence <= last:
            raise ValueError('repeated or unordered original frame ID')
        last = sequence
        saved, previous = previous, {}
        t, tid = action['ts'], action['tid']
        if tid not in pipeline:
            continue
        owner = _cover(pipeline[tid], t, t)
        if owner is None:
            continue
        report, activation_start = owner
        info = report.get('args', {}).get('frame_reporter', {})
        if (info.get('layer_tree_host_id') != tree_id or 'frame_type' in info or
                info.get('state') != 'STATE_PRESENTED_ALL'):
            continue
        source = [layers for begin, end, layers in commits.get(info.get('frame_sequence'), ())
                  if report['ts'] <= begin <= end <= activation_start]
        j = bisect_left(ticks, t)
        if (len(source) != 1 or j == len(trees)
                or (i + 1 < len(actions) and ticks[j] >= actions[i + 1]['ts'])):
            continue
        tree = trees[j]['args']['snapshot']
        if tree['active_tree']['source_frame_number'] != sequence:
            continue
        layers = _drawn_layers(tree)
        if not 0 < len(layers) <= MAX_LAYERS or len({layer['layer_id'] for layer in layers}) != len(layers):
            continue
        current = {}
        for layer in layers:
            identity = _ref(layer)
            records = source[0].get(identity[0])
            if records is None or len(records) > 1 or identity in current:
                break
            if records:
                current[identity] = (layer['bounds'], records[0])
            elif identity in saved and saved[identity][0] == layer['bounds']:
                current[identity] = saved[identity]
            else:
                break
        else:
            previous = current
            yield sequence, (tree, layers, {k[0]: v[1] for k, v in current.items()})

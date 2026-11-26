"""Cloud-only equality proof for an offline vendor handoff. Only sizes and equality leave this module."""

import asyncio
from dataclasses import dataclass
import hmac
import os
import socket

from .browser_surface import _CATEGORIES, _capture_owned_viewport, _require, capture_offline_viewport
from .chromium_capture import CaptureError, PICTURE, TREE
from .wire import encode_message


@dataclass(frozen=True)
class OfflinePaintComparison:
    identical: bool
    reference_bytes: int
    candidate_bytes: int


def _is_offline_handoff(attempt):
    # The original URL stays in the browser. Only its fixed destination is tested.
    return (isinstance(attempt, str) and attempt.startswith('https://auth.openai.com/oauth/authorize?')
            and not attempt.endswith('?') and '#' not in attempt)


def _binding_event(event, frame_id):
    name = event.get('name')
    data = event.get('args', {}).get('data', {})
    if name == 'TracingStartedInBrowser':
        frames = [{'frame': frame_id, 'processId': f.get('processId')}
                  for f in data.get('frames', []) if f.get('frame') == frame_id]
        return {'name': name, 'args': {'data': {'frames': frames}}}
    if name == 'FrameCommittedInBrowser' and data.get('frame') == frame_id:
        return {'name': name, 'args': {'data': {'frame': frame_id, 'processId': data.get('processId')}}}
    return None


async def _receive_trace(cdp, categories, keep, *, repaint):
    """Consume CDP events in memory. Never request a stream/file or enable DevTools screenshots."""
    done = asyncio.get_running_loop().create_future()
    events = []
    picture_bytes = 0
    overflow = False

    def receive(batch):
        nonlocal picture_bytes, overflow
        if overflow:
            return
        for item in batch['value']:
            cleaned = keep(item)
            if cleaned is None:
                continue
            if cleaned.get('name') == PICTURE:
                snap = cleaned.get('args', {}).get('snapshot', {})
                picture_bytes += len(snap.get('skp64', ''))
            if picture_bytes > 64 << 20 or len(events) >= 12000:
                overflow = True
                events.clear()
                return
            events.append(cleaned)

    def complete(result):
        if not done.done():
            done.set_result(result)

    cdp.on('Tracing.dataCollected', receive)
    cdp.once('Tracing.tracingComplete', complete)
    try:
        await cdp.send('Tracing.start', {'categories': categories, 'transferMode': 'ReportEvents'})
        try:
            if repaint:
                try:
                    await cdp.send('Overlay.setShowDebugBorders', {'show': True})
                    await asyncio.sleep(.08)
                finally:
                    await cdp.send('Overlay.setShowDebugBorders', {'show': False})
            await asyncio.sleep(.08)
        finally:
            await cdp.send('Tracing.end')
        result = await done
        _require(not overflow and not result.get('dataLossOccurred'), 'incomplete private browser paint')
        return events
    finally:
        cdp.remove_listener('Tracing.dataCollected', receive)
        if not done.done():
            cdp.remove_listener('Tracing.tracingComplete', complete)


async def _private_snapshot(cdp, window):
    def binding(e):
        return _binding_event(e, window.frame_id)

    metadata = await _receive_trace(cdp, '-*,disabled-by-default-devtools.timeline', binding, repaint=False)
    pids = set()
    for event in metadata:
        data = event['args']['data']
        if event['name'] == 'TracingStartedInBrowser':
            pids.update(f['processId'] for f in data['frames'])
        else:
            pids.add(data['processId'])
    _require(len(pids) == 1, 'no unique private renderer')
    renderer = pids.pop()
    _require(type(renderer) is int and renderer > 0, 'invalid private renderer')
    accept = {PICTURE, TREE, 'SetLayerTreeId', 'UpdateLayer', 'Commit', 'Activation', 'ActivateLayerTree', 'PipelineReporter'}

    def drawings(event):
        marker = binding(event)
        if marker is not None:
            return marker
        if event.get('pid') != renderer or event.get('name') not in accept:
            return None
        if event['name'] == 'SetLayerTreeId':
            data = event['args'].get('data', {})
            if data.get('frame') != window.frame_id:
                return None
            return {'name': 'SetLayerTreeId', 'pid': renderer,
                    'args': {'data': {'frame': window.frame_id, 'layerTreeId': data.get('layerTreeId')}}}
        return event

    return await _receive_trace(cdp, _CATEGORIES, drawings, repaint=True)


async def compare_offline_handoff(actual_page, reference_page, *, browser_pid, reference_pid, executable,
                                  on_verified_reference=None):
    """Compare fixed-sequence complete original commands. Neither the actual drawing nor its digest is returned.

    The trusted source owner provides two different fresh dedicated browser processes on
    the same loopback-only Linux network namespace. Only the query-free reference is
    eligible for publication through capture_offline_viewport.
    """
    _require(browser_pid != reference_pid and actual_page.context.browser is not reference_page.context.browser,
             'reference and actual browser must be independent')
    _require([name for _, name in socket.if_nameindex()] == ['lo']
             and all(os.path.samefile('/proc/self/ns/net', f'/proc/{pid}/ns/net') for pid in (browser_pid, reference_pid)),
             'private comparison requires an isolated, loopback-only source')
    _, reference = await capture_offline_viewport(reference_page, 1, browser_pid=reference_pid, executable=executable)
    public = encode_message(reference)
    try:
        _, frame = await _capture_owned_viewport(actual_page, 1, browser_pid, executable,
                                                  accepts_target=_is_offline_handoff, snapshot=_private_snapshot)
        candidate = encode_message(frame)
        result = OfflinePaintComparison(hmac.compare_digest(public, candidate), len(public), len(candidate))
        del candidate, frame
    except Exception:
        # CDP and OS errors can contain private process context. Do not expose them.
        raise CaptureError('private browser drawing could not be verified') from None
    if result.identical and on_verified_reference is not None:
        on_verified_reference(public)
    return result

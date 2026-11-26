"""Read Skia drawing commands from the selected, dedicated cloud Chromium surface.

Chromium's compositor trace describes the active layer tree and the SkPictures used
to paint it. Never guess a layer by size or select a tab by its URL. Callers own
the browser process and hand this module a Page from its private session.
"""
import asyncio
import base64
import binascii
import json
import math
import struct

from .skp_fonts import FontError, embed_typefaces
from .wire import Frame, Layer, MAX_PICTURE, WireError
from .viz_picture import compose, VizError, VizPending


class CaptureError(RuntimeError):
    """This compositor frame could not be faithfully represented on the wire."""


class _UncommittedPicture(CaptureError):
    """Compositor changed its recording during this trace window."""


PICTURE = "cc::DisplayItemList:snapshot"
TREE = "LayerTreeHostImpl:snapshot"
CATEGORIES = "-*,disabled-by-default-devtools.timeline,disabled-by-default-devtools.timeline.layers,disabled-by-default-devtools.timeline.picture,disabled-by-default-viz.quads"
MAX_TRACE_BYTES = 96 * 1024 * 1024


def _drawn_layers(snapshot):
    return [layer for layer in snapshot["active_tree"]["layers"]
            if layer["draws_content"] and layer.get("opacity", 1) > 0
            and layer["bounds"]["width"] and layer["bounds"]["height"]
            and layer.get("visible_rect", [0, 0, 1, 1])[2] > 0
            and layer.get("visible_rect", [0, 0, 1, 1])[3] > 0]


def _ref(layer):
    pictures = layer.get("pictures", [])
    if len(pictures) != 1 or not pictures[0].get("id_ref"):
        raise CaptureError("当前窗口含绘图协议尚不支持的独立图面。")
    return layer["layer_id"], pictures[0]["id_ref"]


def _placement(layer, width, height):
    if (layer.get("opacity") != 1 or layer.get("effect_tree_index") != 1
            or layer.get("clip_tree_index") not in (1, 2) or layer.get("sorting_context_id")):
        raise CaptureError("当前窗口的合成效果或裁剪尚不能完整重绘。")
    x, y, right, y1, right2, bottom, x1, bottom2 = layer["layer_quad"]
    if (not all(math.isfinite(n) for n in layer["layer_quad"])
            or (x, y, right, bottom) != (x1, y1, right2, bottom2) or right <= x or bottom <= y):
        raise CaptureError("当前窗口含旋转或透视图层。")
    layer_w, layer_h = layer["bounds"]["width"], layer["bounds"]["height"]
    sx, sy = (right-x)/layer_w, (bottom-y)/layer_h
    vx, vy, vw, vh = layer["visible_rect"]
    # Viewport clipping is applied by the receiver. A tighter internal clip
    # requires a separate protocol field; silently painting beyond it is wrong.
    if (x + vx*sx > max(0, x) + 0.01 or y + vy*sy > max(0, y) + 0.01
            or x + (vx+vw)*sx < min(width, right) - 0.01
            or y + (vy+vh)*sy < min(height, bottom) - 0.01):
        raise CaptureError("图层在窗口内部另有裁剪。")
    return x, y, sx, sy


def _portable(picture):
    px, py, pw, ph = picture["params"]["layer_rect"]
    data = picture["skp64"]
    if len(data) > 4*((MAX_PICTURE+2)//3):
        raise CaptureError("图层绘图指令超过单层传输上限。")
    skp = base64.b64decode(data, validate=True)
    if (len(skp) < 28 or skp[:8] != b"skiapict"
            or struct.unpack_from("<4f", skp, 12) != (0, 0, pw, ph)):
        raise CaptureError("SkPicture 绘制范围与 Chromium 报告不一致。")
    try:
        portable = embed_typefaces(skp)
    except FontError as exc:
        raise CaptureError("当前应用的云端字体尚不能在本地精确重绘。") from exc
    return px, py, pw, ph, portable


def parse_trace_frame(events, *, process_id, width, height, sequence, layer_tree_id=None):
    """Use the final active tree and only pictures published by its renderer."""
    if not process_id:
        raise CaptureError("未确认所选窗口的 Chromium 渲染进程。")
    try:
        ordered = sorted((e for e in events if e.get("pid") == process_id and
                          e.get("name") in (PICTURE, TREE) and "snapshot" in e.get("args", {})),
                         key=lambda e: e["ts"])
        # A new display list can already be queued for a future commit. Only
        # consume records through the last tree accepted by the compositor.
        last_tree = next((i for i in range(len(ordered)-1, -1, -1)
                          if ordered[i]["name"] == TREE), None)
        if last_tree is None:
            raise _UncommittedPicture("Chromium 未返回已提交的合成图层树。")
        tree_ids = {event["args"].get("layerTreeId") for event in ordered if event["name"] == TREE}
        if (len(tree_ids) != 1 or None in tree_ids
                or (layer_tree_id is not None and layer_tree_id not in tree_ids)):
            raise CaptureError("渲染进程包含多个或未识别的合成图层树。")
        pictures = {}
        anonymous = {}
        final = None
        for event in ordered[:last_tree+1]:
            args = event["args"]
            snapshot = args["snapshot"]
            thread = event["tid"]
            if event["name"] == PICTURE:
                if args.get("id") and args.get("layer_id") is not None:
                    pictures[args["layer_id"], args["id"]] = snapshot
                else:
                    anonymous.setdefault(thread, []).append(snapshot)
                continue
            final = snapshot
            pending = anonymous.pop(thread, [])
            # Chromium 156 omits layer_id on picture events. One compositor
            # picture immediately preceding a one-picture active tree on that
            # SAME thread is unambiguous. Reject all other anonymous groupings.
            layers = _drawn_layers(snapshot)
            if len(pending) == 1 and len(layers) == 1 and layers[0].get("pictures"):
                pictures[_ref(layers[0])] = pending[0]
        if final["device_viewport_size"] != {"width": width, "height": height}:
            raise CaptureError("采集期间窗口像素尺寸发生变化，或显示缩放尚不支持。")

        def get_picture(ref):
            if ref not in pictures:
                raise _UncommittedPicture("当前活动窗口存在无法准确匹配绘图指令的图层。")
            return pictures[ref]

        layers = _drawn_layers(final)
        if not layers:
            raise _UncommittedPicture("原程序尚未提交内容。")
        try:
            simple = [(_ref(layer), _placement(layer, width, height)) for layer in layers]
        except CaptureError:
            if not final.get("frame", {}).get("render_passes"):
                raise
            cache = {}

            def source(ref):
                if ref not in cache:
                    x, y, _, _, portable = _portable(get_picture(ref))
                    cache[ref] = x, y, portable
                return cache[ref]

            try:
                recorded = compose(final, width, height, source)
            except VizPending as pending:
                raise _UncommittedPicture(str(pending)) from pending
            except VizError as viz_error:
                raise CaptureError(str(viz_error)) from viz_error
            return Frame(sequence, width, height, (Layer(0, 0, width, height, recorded),))

        result = []
        for ref, (x, y, sx, sy) in simple:
            px, py, pw, ph, portable = _portable(get_picture(ref))
            result.append(Layer(x + px*sx, y + py*sy, pw*sx, ph*sy, portable))
        return Frame(sequence, width, height, tuple(result))
    except (KeyError, TypeError, ValueError, IndexError, binascii.Error, WireError) as exc:
        raise CaptureError("Chromium 返回了无法完整重绘的图层记录。") from exc


def _target(events, frame_id):
    processes = set()
    for e in events:
        data = e.get("args", {}).get("data", {})
        if e.get("name") == "TracingStartedInBrowser":
            for frame in data.get("frames", []):
                if frame.get("frame") == frame_id:
                    processes.add(frame.get("processId"))
        elif e.get("name") == "FrameCommittedInBrowser" and data.get("frame") == frame_id:
            processes.add(data.get("processId"))
    if len(processes) != 1 or not all(isinstance(p, int) and p > 0 for p in processes):
        raise CaptureError("未能将 Chromium 图层唯一关联到所选窗口。")
    process = processes.pop()
    tree_ids = {e["args"]["data"].get("layerTreeId") for e in events
                if e.get("name") == "SetLayerTreeId" and e.get("pid") == process
                and e.get("args", {}).get("data", {}).get("frame") == frame_id}
    if len(tree_ids) != 1 or None in tree_ids:
        raise CaptureError("未能将合成器的图层树关联到所选窗口。")
    return process, tree_ids.pop()


async def _read_trace(cdp, handle):
    raw = bytearray()
    try:
        while True:
            chunk = await cdp.send("IO.read", {"handle": handle, "size": 256*1024})
            block = chunk["data"]
            raw.extend(base64.b64decode(block) if chunk.get("base64Encoded") else block.encode())
            if len(raw) > MAX_TRACE_BYTES:
                raise CaptureError("Chromium 绘图跟踪超过本帧的内存上限。")
            if chunk.get("eof"):
                break
    finally:
        await cdp.send("IO.close", {"handle": handle})
    return json.loads(raw)["traceEvents"]


async def _snapshot(cdp, *, categories=CATEGORIES):
    complete = asyncio.get_running_loop().create_future()
    def on_complete(event):
        if not complete.done():
            complete.set_result(event)
    cdp.once("Tracing.tracingComplete", on_complete)
    try:
        await cdp.send("Tracing.start", {"categories": categories, "transferMode": "ReturnAsStream",
                                         "streamFormat": "json"})
        try:
            # The ordinary compositor is idle on a stationary page. Request a
            # redraw through its diagnostic switches. These do not modify DOM,
            # scrolling, focus, form state or window size. Borders are switched
            # off again before reading the final active tree.
            try:
                await cdp.send("Overlay.setShowDebugBorders", {"show": True})
                await asyncio.sleep(0.08)
            finally:
                await cdp.send("Overlay.setShowDebugBorders", {"show": False})
            await asyncio.sleep(0.08)
        finally:
            await cdp.send("Tracing.end")
        done = await complete
        if done.get("dataLossOccurred"):
            raise CaptureError("Chromium 未能完整保留本帧的绘图记录。")
        return await _read_trace(cdp, done["stream"])
    finally:
        if not complete.done():
            cdp.remove_listener("Tracing.tracingComplete", on_complete)


def _exclusive_page(page):
    return len(page.context.pages) == 1 and (
        not page.context.browser or len(page.context.browser.contexts) == 1)


async def capture_frame(page, sequence):
    """Capture a current frame without installing code in the page or taking screenshots."""
    if not _exclusive_page(page):
        raise CaptureError("绘图采集需要所选程序独占一个私有浏览器会话。")
    cdp = await page.context.new_cdp_session(page)
    try:
        async with asyncio.timeout(15):
            tree = await cdp.send("Page.getFrameTree")
            frame_id = tree["frameTree"]["frame"]["id"]
            viewport = (await cdp.send("Page.getLayoutMetrics"))["cssVisualViewport"]
            if viewport.get("scale", 1) != 1 or viewport.get("zoom", 1) != 1:
                raise CaptureError("当前窗口的应用缩放比例尚不支持准确的点击映射。")
            size = page.viewport_size or {"width": viewport["clientWidth"], "height": viewport["clientHeight"]}
            await cdp.send("DOM.enable")
            await cdp.send("Overlay.enable")
            # Cold-start animations and the first renderer commit may precede
            # the application's first usable scene. Leave subsequent captures
            # responsive and never select an earlier partial tree.
            deadline = asyncio.get_running_loop().time() + (5 if sequence == 1 else 1.5)
            while True:
                events = await _snapshot(cdp)
                current = await cdp.send("Page.getFrameTree")
                if current["frameTree"]["frame"]["id"] != frame_id or not _exclusive_page(page):
                    raise CaptureError("采集期间发生窗口切换。")
                try:
                    process_id, tree_id = _target(events, frame_id)
                    return parse_trace_frame(events, process_id=process_id, layer_tree_id=tree_id,
                                             width=int(size["width"]), height=int(size["height"]), sequence=sequence)
                except CaptureError as exc:
                    transient = isinstance(exc, _UncommittedPicture)
                    if (not transient and sequence != 1 or isinstance(exc.__cause__, FontError)
                            or asyncio.get_running_loop().time() >= deadline):
                        raise
                    await asyncio.sleep(0.1)
    except TimeoutError as exc:
        raise CaptureError("等待 Chromium 提交绘图指令超时。") from exc
    finally:
        await cdp.detach()

const runtime = new URL('./vendor/', import.meta.url);
const maxSurfacePixels = 16 * 1024 * 1024;
let engine;

async function loadEngine() {
  const loader = document.createElement('script');
  const loaded = new Promise((resolve, reject) => {
    loader.addEventListener('load', resolve, {once: true});
    loader.addEventListener('error', () => reject(new Error('无法加载本地 Skia 引擎')), {once: true});
  });
  loader.src = new URL('canvaskit.js', runtime).href;
  document.head.append(loader);
  try {
    const [response] = await Promise.all([
      fetch(new URL('canvaskit.wasm.gz', runtime), {redirect: 'error'}),
      loaded,
    ]);
    if (!response.ok || !response.body) throw new Error('无法读取本地 Skia 运行库');
    const stream = response.body.pipeThrough(new DecompressionStream('gzip'));
    const blob = await new Response(stream, {headers: {'Content-Type': 'application/wasm'}}).blob();
    const wasm = URL.createObjectURL(blob);
    try {
      return await window.CanvasKitInit({locateFile: () => wasm});
    } finally {
      URL.revokeObjectURL(wasm);
    }
  } finally {
    loader.remove();
  }
}

// The caller gives the canvas a CSS size. The replay surface tracks its size
// independently of the cloud viewport and never requests another frame to zoom.
export async function createPictureRenderer(canvas) {
  engine ??= loadEngine().catch(error => {
    engine = undefined;
    throw error;
  });
  const kit = await engine;
  let surface;
  let frame;
  let resolution;
  let closed = false;

  function disposeFrame(current) {
    for (const layer of current?.layers ?? []) layer.picture.delete();
  }

  function fit(width, height) {
    const scale = Math.min(width / frame.width, height / frame.height);
    return {scale, x: (width - frame.width * scale) / 2, y: (height - frame.height * scale) / 2};
  }

  function redraw() {
    if (closed || !frame) return;
    const rect = canvas.getBoundingClientRect();
    if (rect.width <= 0 || rect.height <= 0) return;
    const density = Math.min(window.devicePixelRatio,
      Math.sqrt(maxSurfacePixels / (rect.width * rect.height)),
      8192 / rect.width, 8192 / rect.height);
    const width = Math.max(1, Math.floor(rect.width * density));
    const height = Math.max(1, Math.floor(rect.height * density));
    if (!surface || canvas.width !== width || canvas.height !== height) {
      surface?.dispose();
      surface = undefined;
      canvas.width = width;
      canvas.height = height;
      surface = kit.MakeSWCanvasSurface(canvas);
      if (!surface) throw new Error('无法创建本机绘图表面');
    }
    const draw = surface.getCanvas();
    const view = fit(rect.width, rect.height);
    draw.clear(kit.TRANSPARENT);
    draw.save();
    try {
      draw.scale(width / rect.width, height / rect.height);
      draw.translate(view.x, view.y);
      draw.scale(view.scale, view.scale);
      draw.clipRect([0, 0, frame.width, frame.height], kit.ClipOp.Intersect, false);
      for (const layer of frame.layers) {
        draw.save();
        try {
          const [left, top, right, bottom] = layer.bounds;
          draw.translate(layer.x, layer.y);
          draw.clipRect([0, 0, layer.width, layer.height], kit.ClipOp.Intersect, false);
          draw.scale(layer.width / (right - left), layer.height / (bottom - top));
          draw.translate(-left, -top);
          draw.drawPicture(layer.picture);
        } finally {
          draw.restore();
        }
      }
    } finally {
      draw.restore();
    }
    surface.flush();
  }

  function watchDensity() {
    resolution?.removeEventListener('change', watchDensity);
    resolution = matchMedia(`(resolution: ${window.devicePixelRatio}dppx)`);
    resolution.addEventListener('change', watchDensity, {once: true});
    redraw();
  }
  const observer = new ResizeObserver(redraw);
  observer.observe(canvas);
  watchDensity();

  return {
    // Input has already passed the wire decoder. Replace the current frame
    // only after every SkPicture has successfully deserialized.
    present(incoming) {
      if (closed) throw new Error('绘图表面已关闭');
      const next = {width: incoming.width, height: incoming.height, layers: []};
      try {
        for (const layer of incoming.layers) {
          const picture = kit.MakePicture(layer.skp);
          if (!picture) throw new Error('本机 Skia 无法读取这幅云端绘图');
          const decoded = {picture, x: layer.x, y: layer.y, width: layer.width, height: layer.height};
          next.layers.push(decoded);
          const bounds = picture.cullRect();
          if (!bounds.every(Number.isFinite) || bounds[2] <= bounds[0] || bounds[3] <= bounds[1]) {
            throw new Error('云端绘图边界无效');
          }
          decoded.bounds = bounds;
        }
      } catch (error) {
        disposeFrame(next);
        throw error;
      }
      const previous = frame;
      frame = next;
      try {
        redraw();
      } catch (error) {
        frame = previous;
        disposeFrame(next);
        throw error;
      }
      disposeFrame(previous);
    },
    position(clientX, clientY) {
      if (closed || !frame) return null;
      const rect = canvas.getBoundingClientRect();
      if (rect.width <= 0 || rect.height <= 0) return null;
      const view = fit(rect.width, rect.height);
      const x = (clientX - rect.left - view.x) / view.scale;
      const y = (clientY - rect.top - view.y) / view.scale;
      return x >= 0 && x < frame.width && y >= 0 && y < frame.height ? {x, y} : null;
    },
    destroy() {
      if (closed) return;
      closed = true;
      observer.disconnect();
      resolution.removeEventListener('change', watchDensity);
      disposeFrame(frame);
      surface?.dispose();
    },
  };
}

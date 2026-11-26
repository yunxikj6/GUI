import {createPictureRenderer} from './renderer.js';

// The Python relay checks CLRP before publishing. Decode with DataView so byte
// order and layer offsets do not depend on the browser's machine architecture.
function decodeFrame(buffer) {
  const data = new DataView(buffer);
  const bytes = new Uint8Array(buffer);
  if (buffer.byteLength < 22 || data.getUint32(0) !== 0x434c5250 ||
      data.getUint8(4) !== 1 || data.getUint8(5) !== 1 || data.getUint16(6) !== 0 ||
      data.getUint32(8) !== buffer.byteLength - 12) throw new Error('frame');
  const sequence = data.getUint32(12);
  const width = data.getUint16(16);
  const height = data.getUint16(18);
  const count = data.getUint16(20);
  if (!sequence || !width || !height || !count || count > 128) throw new Error('frame');
  const layers = [];
  let offset = 22;
  for (let i = 0; i < count; i++) {
    const x = data.getFloat32(offset);
    const y = data.getFloat32(offset + 4);
    const w = data.getFloat32(offset + 8);
    const h = data.getFloat32(offset + 12);
    const length = data.getUint32(offset + 16);
    offset += 20;
    if (length > bytes.length - offset) throw new Error('frame');
    layers.push({x, y, width: w, height: h, skp: bytes.subarray(offset, offset + length)});
    offset += length;
  }
  if (offset !== bytes.length) throw new Error('frame');
  return {sequence, width, height, layers};
}

function clickMessage(sequence, x, y) {
  const buffer = new ArrayBuffer(24);
  const data = new DataView(buffer);
  data.setUint32(0, 0x434c5250);
  data.setUint8(4, 1);
  data.setUint8(5, 2);
  data.setUint32(8, 12);
  data.setUint32(12, sequence);
  data.setFloat32(16, x);
  data.setFloat32(20, y);
  return buffer;
}

const canvas = document.querySelector('canvas');
const status = document.querySelector('[role=status]');

function announce(message) {
  status.textContent = message;
  status.hidden = !message;
}

async function run(token) {
  const tab = crypto.randomUUID().replaceAll('-', '');
  const headers = {'X-Viewer-Token': token, 'X-Viewer-Tab': tab};
  const renderer = await createPictureRenderer(canvas);
  let frame;
  let busy = false;
  let stopped = false;
  const abort = new AbortController();
  const show = async response => {
    if (!response.ok) throw new Error('transport');
    const next = decodeFrame(await response.arrayBuffer());
    if (!frame || next.sequence > frame.sequence) {
      renderer.present(next);
      frame = next;
    }
  };
  const getFrame = (suffix = '') => fetch('/frame' + suffix, {headers, cache: 'no-store', signal: abort.signal});
  await show(await getFrame());
  announce('');
  canvas.focus({preventScroll: true});

  canvas.addEventListener('click', async event => {
    if (event.button !== 0 || stopped || busy) return;
    const point = renderer.position(event.clientX, event.clientY);
    if (!point) return;
    busy = true;
    canvas.setAttribute('aria-busy', 'true');
    const waiting = window.setTimeout(() => announce('正在等待云端确认点击…'), 450);
    try {
      const response = await fetch('/click', {
        method: 'POST',
        headers: {...headers, 'Content-Type': 'application/octet-stream'},
        body: clickMessage(frame.sequence, point.x, point.y),
      });
      if (response.status === 409) {
        await show(await getFrame());
        announce('窗口已更新，请重新点击。');
      } else if (response.status !== 204) {
        throw new Error('unconfirmed');
      } else {
        announce(stopped ? '云端连接已断开，已保留当前画面。' : '');
      }
    } catch {
      stopped = true;
      announce('未收到云端操作确认。已保留当前画面，请重新连接后检查操作结果。');
    } finally {
      window.clearTimeout(waiting);
      canvas.removeAttribute('aria-busy');
      busy = false;
    }
  });

  window.addEventListener('pagehide', event => {
    if (!event.persisted) {
      stopped = true;
      abort.abort();
      fetch('/leave', {method: 'POST', headers, keepalive: true}).catch(() => {});
      renderer.destroy();
    }
  });
  while (!stopped) {
    try {
      const update = await getFrame('?after=' + frame.sequence);
      if (stopped) break;
      if (update.status !== 204) await show(update);
    } catch {
      if (!stopped) {
        stopped = true;
        if (!busy) announce('云端连接已断开，已保留当前画面。');
      }
    }
  }
}

function begin(token) {
  if (typeof token !== 'string' || !/^[A-Za-z0-9_-]{43}$/.test(token)) return false;
  delete window.receiveWorkspaceDisplayToken;
  run(token).catch(() => announce('无法重绘云端窗口。请检查绘图协议和本地渲染组件后重新连接。'));
  return true;
}

if (location.hash) {
  const manualToken = location.hash.slice(1);
  history.replaceState(null, '', location.pathname);
  if (!begin(manualToken)) announce('请从薄客户端启动器重新打开此窗口。');
} else {
  // The app host delivers the in-memory capability through its private pipe.
  window.receiveWorkspaceDisplayToken = begin;
  announce('正在等待本机启动器连接。若已刷新窗口，请从启动器重新打开。');
}

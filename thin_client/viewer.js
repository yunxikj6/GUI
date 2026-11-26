import {createPictureRenderer} from './renderer.js';
import {attachKeyboard} from './input.js';

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
  let stopped = false;
  let active;
  let change;
  let generation = 0;
  let notice = 0;
  const pending = [];
  const abort = new AbortController();
  const warn = message => { notice++; announce(message); };
  const discard = () => { generation++; pending.length = 0; };
  const wake = () => { if (change) change(); };
  const show = async response => {
    if (!response.ok) throw new Error('transport');
    const next = decodeFrame(await response.arrayBuffer());
    if (!frame || next.sequence > frame.sequence) {
      renderer.present(next);
      frame = next;
      wake();
    }
  };
  const getFrame = (suffix = '') => fetch('/frame' + suffix, {headers, cache: 'no-store', signal: abort.signal});
  await show(await getFrame());
  announce('');

  async function afterInput(expected) {
    const end = performance.now() + 30000;
    while (!stopped && frame.sequence < expected && performance.now() < end) {
      await new Promise(resolve => {
        const deadline = setTimeout(resolve, Math.max(0, end - performance.now()));
        change = () => { clearTimeout(deadline); resolve(); };
      });
      change = undefined;
    }
    return !stopped && frame.sequence === expected;
  }

  async function drain() {
    while (!stopped && pending.length) {
      const event = pending.shift();
      const displayed = frame.sequence;
      const sequence = event.sequence ?? displayed;
      active = true;
      const originalNotice = notice;
      canvas.setAttribute('aria-busy', 'true');
      const waiting = setTimeout(() => {
        if (notice === originalNotice) announce('正在等待云端确认输入…');
      }, 450);
      let acknowledged = false;
      try {
        const isClick = event.type === 'click';
        const response = await fetch(isClick ? '/click' : '/input', {
          method: 'POST',
          headers: {...headers, 'X-Wait-For-Scene': '1', 'Content-Type': isClick ? 'application/octet-stream' : 'application/json'},
          body: isClick ? clickMessage(sequence, event.x, event.y) : JSON.stringify({...event, sequence}),
        });
        if (response.status === 409) {
          discard();
          await show(await getFrame());
          warn('云端窗口或焦点已变化，本次操作及排队的输入未执行。请确认后继续。');
          return;
        }
        if (response.status === 202) {
          acknowledged = true;
          throw new Error('missing scene after ACK');
        }
        if (response.status !== 204) throw new Error('unconfirmed');
        acknowledged = true;
        // A frame already painted before the explicit input ACK cannot prove
        // which target should receive the next queued keyboard event.
        const expected = Number(response.headers.get('X-Next-Scene'));
        if (!Number.isSafeInteger(expected) || expected <= sequence) throw new Error('invalid next scene');
        if (!await afterInput(expected)) {
          discard();
          if (frame.sequence < expected) stopped = true;
          warn('云端已接收本次操作，但下一张画面未能确认。后续输入未发送。');
          return;
        }
        if (pending.length && expected !== displayed + 1) {
          discard();
          warn('云端已接收本次操作。期间画面发生了变化，排队的输入未发送。');
          return;
        }
        if (notice === originalNotice) announce('');
      } catch {
        stopped = true;
        discard();
        warn(acknowledged ? '云端已接收操作。连接已断开，后续输入未发送。' :
          '未收到云端操作确认。已保留当前画面，请重新连接后检查操作结果。');
      } finally {
        clearTimeout(waiting);
        active = undefined;
        canvas.removeAttribute('aria-busy');
      }
    }
  }

  function enqueue(event) {
    if (stopped) { warn('云端连接已断开，此次输入未发送。'); return; }
    if (pending.length >= 32) { warn('输入队列已满。请等云端确认后再输入。'); return; }
    if (active && event.type === 'click') {
      warn('请等云端确认后，再点击新画面。');
      return;
    }
    generation++;
    pending.push(event);
    if (!active) drain();
  }
  const field = document.querySelector('#keyboard');
  // A new composition can be tied only to a displayed, settled input target.
  const anchor = () => !active && !stopped ? {sequence: frame.sequence, generation} : undefined;
  attachKeyboard(field, canvas, enqueue, warn, anchor, () => generation);
  canvas.addEventListener('click', event => {
    if (event.button !== 0) return;
    const point = renderer.position(event.clientX, event.clientY);
    if (point) enqueue({type: 'click', ...point});
  });
  window.addEventListener('blur', () => {
    const unsent = pending.length;
    discard();
    if (unsent) warn('本机窗口已失去焦点，排队的输入未发送。');
  });
  window.addEventListener('pagehide', event => {
    if (!event.persisted) {
      stopped = true;
      discard();
      wake();
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
        discard();
        wake();
        if (!active) warn('云端连接已断开，已保留当前画面。');
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

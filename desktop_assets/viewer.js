import RFB from './novnc/core/rfb.js';

const byId = id => document.getElementById(id);
const screen = byId('screen');
const status = byId('status');
const control = byId('control');
const mode = byId('mode');
const desktopScale = byId('desktopScale');
const panView = byId('panView');
const panHint = byId('panHint');
const textForm = byId('textForm');
const typing = byId('typing');
const typingStatus = byId('typingStatus');
const typeSubmit = textForm.querySelector('button[type=submit]');
let inputLease = 0, typingRevision = 0, typingBusy = false, typingComposing = false;

function syncTypeSubmit() {
  typeSubmit.disabled = typingBusy || !typing.value || typingComposing;
}

function clearFrameText() {
  if (document.activeElement === typing) typing.blur();
  typing.value = ''; typingStatus.textContent = '';
  ++typingRevision; typingBusy = typingComposing = false; syncTypeSubmit();
}

typing.onkeydown = e => {
  if (e.key === 'Enter' && !e.shiftKey && !typingComposing && !e.isComposing && e.keyCode !== 229) {
    e.preventDefault();
    if (!e.repeat) textForm.requestSubmit();
  }
};
typing.oninput = () => {
  ++typingRevision; typingStatus.textContent = ''; syncTypeSubmit();
};
typing.addEventListener('compositionstart', () => { typingComposing = true; syncTypeSubmit(); });
typing.addEventListener('compositionend', () => { typingComposing = false; syncTypeSubmit(); });
// Keep a pointer press on Send from blurring the field and committing the unfinished syllable.
textForm.addEventListener('pointerdown', e => {
  if (typingComposing && e.target === typeSubmit) e.preventDefault();
}, true);
typeSubmit.addEventListener('click', e => { if (e.detail > 1) e.preventDefault(); });
const clipboardToggle = byId('clipboardToggle');
const clipboardPanel = byId('clipboardPanel');
const sendText = byId('clipboardSendText');
const remoteText = byId('clipboardRemoteText');
const clipboardSend = byId('clipboardSend');
const clipboardCopy = byId('clipboardCopy');
const clipboardStatus = byId('clipboardStatus');
let session = 0, rfb, timer, frameUrl, desktop, controlled = false, stills = false;
let liveConnected = false, clipboardVersion = 0, receivedText = '';

let panRequested = false, panDrag = null;
let visibleSurface = null, viewLeft = 0, viewTop = 0;

// noVNC removes its canvas before emitting disconnect. Save offsets while the source still exists.
function rememberView() {
  if (screen.dataset.scale === 'original' && visibleSurface && screen.contains(visibleSurface)) {
    viewLeft = screen.scrollLeft; viewTop = screen.scrollTop;
  }
}
screen.addEventListener('scroll', rememberView);

function releaseSurface() {
  rememberView();
  visibleSurface = null; panDrag = null;
}

function restoreView() {
  if (visibleSurface && desktopScale.value === 'original') {
    // Native scrolling clamps both axes to this surface and the current viewport.
    screen.scrollTo(viewLeft, viewTop);
    rememberView();
  }
}

function syncPan() {
  const original = desktopScale.value === 'original';
  const local = original && (!controlled || panRequested);
  screen.dataset.scale = desktopScale.value;
  screen.dataset.localPan = String(local);
  const display = screen.firstElementChild;
  panView.hidden = !display || !original || !controlled;
  panView.setAttribute('aria-pressed', String(original && panRequested));
  panHint.hidden = !display || !original;
  panHint.textContent = local ? '拖动或滑动画面可移动本地视窗，聚焦后也可用方向键。' : '点击或滚动画面可操作云端。要移动视窗，请开启本地平移。';
  if (local && screen.contains(document.activeElement)) screen.focus({preventScroll:true});
  if (display) display.inert = local;
}

desktopScale.onchange = () => {
  rememberView();
  syncPan();
  const fit = desktopScale.value === 'fit';
  if (rfb) rfb.scaleViewport = fit;
  if (fit) screen.scrollTo(0, 0);
  else restoreView();
};
panView.onclick = () => {
  panRequested = !panRequested;
  syncPan();
  if (panRequested) screen.focus({preventScroll:true});
};

// Pan locally so a swipe at the edge does not trigger browser history navigation.
screen.addEventListener('pointerdown', e => {
  if (screen.dataset.localPan !== 'true' || !e.isPrimary || e.button !== 0 || e.target !== screen) return;
  const rect = screen.getBoundingClientRect();
  if (e.clientX >= rect.left + screen.clientWidth || e.clientY >= rect.top + screen.clientHeight) return;
  panDrag = {id:e.pointerId, x:e.clientX, y:e.clientY, left:screen.scrollLeft, top:screen.scrollTop};
  screen.focus({preventScroll:true});
  screen.setPointerCapture(e.pointerId);
  e.preventDefault();
});
screen.addEventListener('pointermove', e => {
  if (panDrag?.id === e.pointerId && screen.dataset.localPan === 'true') {
    screen.scrollTo(panDrag.left + panDrag.x - e.clientX, panDrag.top + panDrag.y - e.clientY);
    rememberView();
  }
});
screen.addEventListener('lostpointercapture', () => { panDrag = null; });

function canExchange() {
  return liveConnected && controlled && !stills && rfb && !rfb.viewOnly;
}

function clipboardSendIssue(client, text) {
  // Bundled noVNC 1.7.0 exposes only power via its public capabilities.
  // Match its Text + Notify branch; Provide is needed to answer the server's request.
  const actions = client._clipboardServerCapabilitiesActions;
  if (client._clipboardServerCapabilitiesFormats?.[1] && actions?.[1 << 27]) {
    return actions[1 << 28] ? '' : '云端尚未确认可以接收剪贴板正文，原文已保留。请稍后重试。';
  }
  // The classic ClientCutText encoder replaces every code point above Latin-1 with '?'.
  return /[^\u0000-\u00ff]/u.test(text)
    ? '此连接尚未确认扩展剪贴板支持，目前只能发送 Latin-1 字符。原文已保留，可稍后重试。' : '';
}

function syncClipboard() {
  clipboardToggle.hidden = !canExchange();
  if (!clipboardToggle.hidden) return;
  ++clipboardVersion;
  receivedText = ''; sendText.value = ''; remoteText.value = '';
  clipboardStatus.textContent = ''; clipboardSend.disabled = clipboardCopy.disabled = true;
  clipboardPanel.hidden = true; clipboardToggle.setAttribute('aria-expanded','false');
}

clipboardToggle.onclick = () => {
  if (!canExchange()) return;
  clipboardPanel.hidden = !clipboardPanel.hidden;
  clipboardToggle.setAttribute('aria-expanded',String(!clipboardPanel.hidden));
  if (clipboardPanel.hidden) ++clipboardVersion;
  else sendText.focus();
};
sendText.oninput = () => { clipboardSend.disabled = !canExchange() || !sendText.value; };
clipboardSend.onclick = () => {
  if (!canExchange() || clipboardPanel.hidden || !sendText.value) return;
  ++clipboardVersion;
  const issue = clipboardSendIssue(rfb, sendText.value);
  if (issue) { clipboardStatus.textContent = issue; return; }
  try {
    rfb.clipboardPasteFrom(sendText.value);
    clipboardStatus.textContent = '已提供剪贴板文本，请在云端应用中粘贴。';
  } catch {
    clipboardStatus.textContent = '发送失败，请检查实时连接后重试。';
  }
};
clipboardCopy.onclick = async () => {
  if (!canExchange() || clipboardPanel.hidden || !receivedText) return;
  const version = ++clipboardVersion;
  try {
    await navigator.clipboard.writeText(receivedText);
    if (version === clipboardVersion) clipboardStatus.textContent = '已复制到本机剪贴板。';
  } catch {
    if (version !== clipboardVersion || clipboardPanel.hidden) return;
    remoteText.focus(); remoteText.select();
    clipboardStatus.textContent = '已选中文本，请使用系统的“复制”命令。';
  }
};

async function call(action, body) {
  const opts = {method: 'POST'};
  if (body) Object.assign(opts, {headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)});
  const response = await fetch('/desktop/' + action, opts);
  const data = await response.json();
  if (!response.ok) {
    const message = typeof data.error === 'string' ? data.error : data.error?.message;
    const error = Error(message || '云端请求失败');
    error.status = response.status;
    throw error;
  }
  return data;
}

function cleanup() {
  releaseSurface();
  ++session;
  liveConnected = false; syncClipboard();
  clearTimeout(timer);
  if (rfb) { rfb.disconnect(); rfb = null; }
  if (frameUrl) { URL.revokeObjectURL(frameUrl); frameUrl = null; }
  screen.replaceChildren(); syncPan(); clearFrameText();
  mode.disabled = true; control.disabled = true; textForm.hidden = true;
  return session;
}

function syncControl(state) {
  // Other tabs and sessions can hold this account's lease. Only a takeover here enables input.
  controlled = controlled && Boolean(state.yours && state.controller === 'human');
  control.disabled = state.kind === 'x11' && state.agentPausable === false;
  control.textContent = controlled ? '交还输入' : '接管输入';
  if (rfb) rfb.viewOnly = !controlled;
  if (!controlled) {
    ++inputLease; panRequested = false; clearFrameText();
  }
  syncPan();
  textForm.hidden = !(controlled && stills);
  syncClipboard();
}

function openLiveSocket(n) {
  // A noVNC instance retains its offered text for delayed clipboard requests.
  // Retire the old connection when ownership changes so it cannot answer a later lease.
  const previous = rfb;
  releaseSurface();
  rfb = null; liveConnected = false; syncClipboard();
  if (previous) previous.disconnect();
  const url = new URL('/desktop/vnc', location.href);
  url.protocol = location.protocol === 'https:' ? 'wss:' : 'ws:';
  const client = new RFB(screen, url.href);
  rfb = client;
  const current = () => n === session && client === rfb;
  client.scaleViewport = desktopScale.value === 'fit'; client.resizeSession = false; client.viewOnly = !controlled;
  client.addEventListener('connect', () => {
    if (!current()) return;
    liveConnected = true; syncPan(); syncClipboard();
    status.textContent = controlled ? '已接管输入' : '正在观看';
    visibleSurface = screen.querySelector('canvas'); restoreView();
  });
  client.addEventListener('clipboard', e => {
    if (!current() || !canExchange() || typeof e.detail?.text !== 'string') return;
    ++clipboardVersion;
    receivedText = e.detail.text; remoteText.value = receivedText;
    clipboardCopy.disabled = !receivedText;
    clipboardStatus.textContent = receivedText ? '已收到云端剪贴板文本。' : '云端剪贴板为空。';
  });
  client.addEventListener('disconnect', () => {
    if (!current()) return;
    rfb = null; liveConnected = false; syncClipboard();
    if (!mode.disabled) { showFrames(); return; }
    status.textContent = '图形连接已断开，请重新连接。';
  });
}

async function connect() {
  const n = cleanup();
  stills = false; desktop = null;
  status.textContent = '启动桌面中…';
  mode.textContent = '逐帧查看';
  try {
    const s = await call('start');
    if (n !== session) return;
    desktop = s; syncControl(s); mode.disabled = desktop.kind !== 'x11' && !!desktop.kind;
    status.textContent = '连接图形会话…';
    openLiveSocket(n);
  } catch (e) {
    if (n === session) status.textContent = e.message;
  }
}

async function showFrames() {
  const n = cleanup();
  stills = true; mode.disabled = false; mode.textContent = '切回实时';
  syncControl(desktop); status.textContent = '连接逐帧模式…';
  const img = new Image(); img.tabIndex = 0; img.draggable = false; img.alt = '云端 Linux 屏幕';
  screen.replaceChildren(img); syncPan();
  img.addEventListener('load', () => {
    if (n === session) { visibleSurface = img; restoreView(); }
  }, {once:true});
  let reading = false, frameWidth = 0, frameHeight = 0;
  async function refresh() {
    clearTimeout(timer);
    if (reading || n !== session) return;
    reading = true;
    try {
      if (!document.hidden) {
        const r = await fetch('/desktop/frame', {cache:'no-store'});
        if (!r.ok) throw Error((await r.json()).error || '获取画面失败');
        const blob = await r.blob();
        if (n !== session) return;
        const nextUrl = URL.createObjectURL(blob);
        try {
          // Keep the previous frame and its input coordinates available while decoding.
          const next = new Image(); next.src = nextUrl;
          await next.decode();
          if (n !== session) return;
          const old = frameUrl;
          frameWidth = next.naturalWidth; frameHeight = next.naturalHeight;
          frameUrl = nextUrl; img.src = nextUrl;
          if (old) URL.revokeObjectURL(old);
        } finally {
          if (frameUrl !== nextUrl) URL.revokeObjectURL(nextUrl);
        }
        status.textContent = controlled ? '逐帧控制' : '逐帧观看';
      }
    } catch(e) { if (n === session) status.textContent = e.message; }
    finally { reading = false; if (n === session) timer = setTimeout(refresh, 2200); }
  }
  let chain = Promise.resolve();
  const sendInput = payload => {
    if (!controlled || n !== session) return Promise.resolve();
    const lease = inputLease, revision = typingRevision;
    chain = chain.then(async () => {
      if (!controlled || n !== session || lease !== inputLease) return;
      try {
        const response = await call('input', payload);
        if (payload.key === 'type' && response?.ok !== true) throw Error('未收到逐帧输入回执。');
      } catch(e) {
        if (n !== session || lease !== inputLease) return;
        if (e.status === 409) { desktop = {...desktop, yours:false}; syncControl(desktop); }
        if (payload.key === 'type' && controlled) {
          typingStatus.textContent = [400, 422].includes(e.status) ? e.message :
            '无法确认文字是否已送达。请先查看云端窗口，再决定是否重发。';
        }
        status.textContent = e.message;
        return;
      }
      if (n === session && lease === inputLease) {
        if (payload.key === 'type' && typingRevision === revision) {
          typing.value = '';
          typingStatus.textContent = '已发出按键，请在云端窗口核对文字。';
        }
        await refresh();
      }
    });
    return chain;
  };
  function sendClick(e, button) {
    const r = img.getBoundingClientRect();
    if (!r.width || !r.height || !frameWidth || !frameHeight) return;
    const x = Math.floor((e.clientX-r.x)*frameWidth/r.width);
    const y = Math.floor((e.clientY-r.y)*frameHeight/r.height);
    if (x >= 0 && x < frameWidth && y >= 0 && y < frameHeight) sendInput({key:'click',button,x,y});
  }
  img.onclick = e => { img.focus({preventScroll:true}); sendClick(e, 1); };
  img.oncontextmenu = e => { e.preventDefault(); sendClick(e, 3); };
  let wheelAt = 0;
  img.onwheel = e => {
    if (!controlled || screen.dataset.localPan === 'true') return;
    e.preventDefault(); if (!e.deltaY || performance.now() - wheelAt < 150) return;
    wheelAt = performance.now(); sendClick(e, e.deltaY<0?4:5);
  };
  img.onkeydown = e => {
    if (!controlled || screen.dataset.localPan === 'true' || e.isComposing || e.repeat) return;
    const names = {Enter:'Return',Backspace:'BackSpace',Escape:'Escape',Tab:'Tab',ArrowUp:'Up',ArrowDown:'Down',ArrowLeft:'Left',ArrowRight:'Right',Delete:'Delete',Home:'Home',End:'End',' ':'space'};
    const key = names[e.key] || (/^[A-Za-z0-9]$/.test(e.key) ? e.key : null);
    if (!key) return;
    e.preventDefault();
    sendInput({key:'press',value:[e.ctrlKey?'ctrl':'',e.altKey?'alt':'',e.shiftKey?'shift':'',key].filter(Boolean).join('+')});
  };
  textForm.onsubmit = async e => {
    e.preventDefault();
    if (typeSubmit.disabled || typingComposing || !controlled || n !== session || !typing.value) return;
    const text = typing.value;
    if (/[^ -~]/u.test(text)) {
      typingStatus.textContent = '整段文字未发送。逐帧输入仅支持单行可打印 ASCII。请先复制本框内容，切回实时桌面，打开“剪贴板”发送后在云端粘贴。';
      return;
    }
    if (text.length > 2048) { typingStatus.textContent = '整段文字未发送。请将每次输入控制在 2048 个字符内。'; return; }
    const lease = inputLease;
    typingBusy = true; syncTypeSubmit(); typingStatus.textContent = '正在发送…';
    try { await sendInput({key:'type',value:text}); }
    finally { if (n === session && lease === inputLease) { typingBusy = false; syncTypeSubmit(); } }
  };
  await refresh();
}

control.onclick = async () => {
  const n = session;
  const releasing = controlled;
  if (releasing) { controlled = false; syncControl(desktop); }
  control.disabled = true;
  try {
    const state = await call(releasing ? 'handback' : 'takeover');
    if (n !== session) return;
    controlled = !releasing;
    desktop = state; syncControl(state);
    status.textContent = controlled ? '已接管输入' : '正在观看';
    if (releasing && rfb) openLiveSocket(n);
  } catch(e) {
    if (n !== session) return;
    if (releasing) { connect(); return; }
    control.disabled = false; status.textContent = e.message;
  }
};
mode.onclick = () => { if (stills) connect(); else showFrames(); };
byId('retry').onclick = connect;
window.addEventListener('pagehide', () => { controlled = false; cleanup(); });
window.addEventListener('pageshow', e => { if (e.persisted) connect(); });
connect();

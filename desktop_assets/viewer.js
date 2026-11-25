import RFB from './novnc/core/rfb.js';

const byId = id => document.getElementById(id);
const screen = byId('screen');
const status = byId('status');
const control = byId('control');
const mode = byId('mode');
const textForm = byId('textForm');
const clipboardToggle = byId('clipboardToggle');
const clipboardPanel = byId('clipboardPanel');
const sendText = byId('clipboardSendText');
const remoteText = byId('clipboardRemoteText');
const clipboardSend = byId('clipboardSend');
const clipboardCopy = byId('clipboardCopy');
const clipboardStatus = byId('clipboardStatus');
let session = 0, rfb, timer, frameUrl, desktop, controlled = false, stills = false;
let liveConnected = false, clipboardVersion = 0, receivedText = '';

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
  ++session;
  liveConnected = false; syncClipboard();
  clearTimeout(timer);
  if (rfb) { rfb.disconnect(); rfb = null; }
  if (frameUrl) { URL.revokeObjectURL(frameUrl); frameUrl = null; }
  screen.replaceChildren();
  mode.disabled = true; control.disabled = true; textForm.hidden = true;
  return session;
}

function syncControl(state) {
  controlled = Boolean(state.yours && state.controller === 'human');
  control.disabled = state.kind === 'x11' && state.agentPausable === false;
  control.textContent = controlled ? '交还输入' : '接管输入';
  if (rfb) rfb.viewOnly = !controlled;
  textForm.hidden = !(controlled && stills);
  syncClipboard();
}

function openLiveSocket(n) {
  // A noVNC instance retains its offered text for delayed clipboard requests.
  // Retire the old connection when ownership changes so it cannot answer a later lease.
  const previous = rfb;
  rfb = null; liveConnected = false; syncClipboard();
  if (previous) previous.disconnect();
  const url = new URL('/desktop/vnc', location.href);
  url.protocol = location.protocol === 'https:' ? 'wss:' : 'ws:';
  const client = new RFB(screen, url.href);
  rfb = client;
  const current = () => n === session && client === rfb;
  client.scaleViewport = true; client.resizeSession = false; client.viewOnly = !controlled;
  client.addEventListener('connect', () => {
    if (!current()) return;
    liveConnected = true; syncClipboard();
    status.textContent = controlled ? '已接管输入' : '正在观看';
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
  stills = false; controlled = false; desktop = null;
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
  const img = new Image(); img.tabIndex = 0; img.alt = '云端 Linux 屏幕';
  screen.replaceChildren(img);
  let reading = false;
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
        const old = frameUrl; frameUrl = URL.createObjectURL(blob); img.src = frameUrl;
        if (old) URL.revokeObjectURL(old);
        status.textContent = controlled ? '逐帧控制' : '逐帧观看';
      }
    } catch(e) { if (n === session) status.textContent = e.message; }
    finally { reading = false; if (n === session) timer = setTimeout(refresh, 2200); }
  }
  let chain = Promise.resolve();
  const sendInput = payload => {
    if (!controlled || n !== session) return Promise.resolve();
    chain = chain.then(async () => {
      if (!controlled || n !== session) return;
      try { await call('input', payload); await refresh(); }
      catch(e) {
        if (n === session) {
          if (e.status === 409) { desktop = {...desktop, yours:false}; syncControl(desktop); }
          status.textContent = e.message;
        }
      }
    });
    return chain;
  };
  function point(e) {
    const r = img.getBoundingClientRect();
    return {x:Math.floor((e.clientX-r.x)*img.naturalWidth/r.width),y:Math.floor((e.clientY-r.y)*img.naturalHeight/r.height)};
  }
  img.onclick = e => { img.focus(); sendInput({key:'click',button:1,...point(e)}); };
  img.oncontextmenu = e => { e.preventDefault(); sendInput({key:'click',button:3,...point(e)}); };
  let wheelAt = 0;
  img.onwheel = e => {
    if (!controlled || !e.deltaY) return;
    e.preventDefault(); if (performance.now() - wheelAt < 150) return;
    wheelAt = performance.now(); sendInput({key:'click',button:e.deltaY<0?4:5,...point(e)});
  };
  img.onkeydown = e => {
    if (!controlled || e.isComposing || e.repeat) return;
    const names = {Enter:'Return',Backspace:'BackSpace',Escape:'Escape',Tab:'Tab',ArrowUp:'Up',ArrowDown:'Down',ArrowLeft:'Left',ArrowRight:'Right',Delete:'Delete',Home:'Home',End:'End',' ':'space'};
    const key = names[e.key] || (/^[A-Za-z0-9]$/.test(e.key) ? e.key : null);
    if (!key) return;
    e.preventDefault();
    sendInput({key:'press',value:[e.ctrlKey?'ctrl':'',e.altKey?'alt':'',e.shiftKey?'shift':'',key].filter(Boolean).join('+')});
  };
  textForm.onsubmit = async e => {
    e.preventDefault(); const input = byId('typing'); const text = input.value;
    if (text) { await sendInput({key:'type',value:text}); }
  };
  await refresh();
}

control.onclick = async () => {
  const n = session;
  const releasing = controlled;
  control.disabled = true;
  if (releasing && rfb) { rfb.viewOnly = true; syncClipboard(); }
  try {
    const state = await call(releasing ? 'handback' : 'takeover');
    if (n !== session) return;
    desktop = state; syncControl(state);
    status.textContent = controlled ? '已接管输入' : '正在观看';
    if (releasing && rfb) openLiveSocket(n);
  } catch(e) {
    if (n !== session) return;
    if (releasing && rfb) { connect(); return; }
    control.disabled = false; status.textContent = e.message;
  }
};
mode.onclick = () => { if (stills) connect(); else showFrames(); };
byId('retry').onclick = connect;
window.addEventListener('pagehide', () => { cleanup(); });
window.addEventListener('pageshow', e => { if (e.persisted) connect(); });
connect();

'use strict';

const $ = id => document.getElementById(id);
const names = { check: '项目检查', codex: 'Codex', claude: 'Claude Code' };
const labels = { queued: '排队中', running: '运行中', succeeded: '已完成', failed: '失败' };
const isActive = task => ['queued', 'running'].includes(task?.status);
const drafts = new Map();
// Only fetched files live here. Edited documents retain their own base revision.
const fileCache = new Map();
const CACHE_ENTRIES = 24;
const CACHE_BYTES = 4 * 1024 * 1024;
let cachedBytes = 0;
function cacheWeight(file) { return 2 * (file.path.length + file.content.length) + 256; }
function forgetFile(path) {
  const item = fileCache.get(path);
  if (item) cachedBytes -= cacheWeight(item);
  fileCache.delete(path);
}
function cacheFile(file) {
  forgetFile(file.path);
  const weight = cacheWeight(file);
  if (weight > CACHE_BYTES) return;
  fileCache.set(file.path, file);
  cachedBytes += weight;
  while (fileCache.size > CACHE_ENTRIES || cachedBytes > CACHE_BYTES) forgetFile(fileCache.keys().next().value);
}
function cachedFile(path) {
  const file = fileCache.get(path);
  if (file) { fileCache.delete(path); fileCache.set(path,file); }
  return file;
}
function fileSync(message = '', stale = false) {
  $('fileSync').textContent = message; $('fileSync').dataset.stale = String(stale); $('fileSync').removeAttribute('title');
}
const state = {
  project: null, providers: {}, ready: false, view: 'files', file: null,
  fileVersion: 0, treeVersion: 0, refreshVersion: 0, taskListVersion: 0, taskVersion: 0,
  tasks: [], selectedTask: null, cursor: 0, timer: null, eventsSize: 0,
  selection: null, sending: false, pending: null, retryCount: 0, loadedKey: null,
};
function el(tag, cls, content) {
  const node = document.createElement(tag);
  if (cls) node.className = cls;
  if (content !== undefined) node.textContent = content;
  return node;
}
function stored(key, fallback = '') {
  try { return localStorage.getItem('cloudwork.' + key) ?? fallback; }
  catch { return fallback; }
}
function remember(key, value) {
  try {
    if (value === null) localStorage.removeItem('cloudwork.' + key);
    else localStorage.setItem('cloudwork.' + key, value);
  } catch { /* Storage can be unavailable in private browsing. */ }
}
function key(suffix) { return state.loadedKey + '.' + suffix; }
function connection(mode) {
  $('connection').dataset.state = mode;
  $('connectionLabel').textContent = {loading:'连接中', online:state.mode === 'local' ? '本机模式' : '云机已连接', error:'连接中断'}[mode];
}
async function rpc(op, fields = {}) {
  let res, envelope;
  try {
    res = await fetch('/api/rpc', {
      method:'POST', headers:{'Content-Type':'application/json'},
      credentials:'same-origin', cache:'no-store', body:JSON.stringify({op, ...fields}),
      signal:AbortSignal.timeout(90000),
    });
    envelope = await res.json();
  } catch {
    connection('error');
    const err = new Error('与工作区的连接中断。恢复网络后可重试。');
    err.code = 'transport';
    throw err;
  }
  if (!res.ok || !envelope.ok) {
    const err = new Error(envelope.error?.message || '工作区请求失败');
    err.code = envelope.error?.code || 'transport';
    if (res.status >= 500 || err.code === 'transport') connection('error');
    throw err;
  }
  connection('online');
  return envelope.data;
}
function asError(err) { return String(err?.message || '操作失败').slice(0, 1200); }
function mobilePage(page) {
  $('workbench').dataset.mobile = page;
  $('mobileFiles').setAttribute('aria-pressed', String(page === 'files'));
  $('mobileTasks').setAttribute('aria-pressed', String(page === 'tasks'));
}
function viewButtons(view) {
  state.view = view;
  for (const [id, name] of [['showFiles','files'],['showDiff','diff']]) {
    $(id).classList.toggle('selected', name === view);
    $(id).setAttribute('aria-pressed', String(name === view));
  }
  for (const btn of $('tree').querySelectorAll('[data-file]')) {
    btn.setAttribute('aria-current', String(view === 'files' && btn.dataset.file === state.selection));
  }
}
function prettySize(size) {
  return size < 1024 ? size + ' B' : (size / 1024).toFixed(1) + ' KiB';
}
function fileHeading(label, title, path = '', size = '') {
  $('viewLabel').textContent = label;
  $('editorTab').textContent = title;
  $('fileTitle').textContent = title;
  $('filePath').textContent = path;
  $('fileSize').textContent = size;
}
function resetPreview() {
  $('preview').replaceChildren(); $('fileNotice').textContent = '';
  $('editBar').hidden = true; $('fileEditArea').hidden = true;
  state.file = null; fileSync();
}
function drawLines(value, diff) {
  const preview = $('preview');
  const lines = String(value).slice(0, 180000).split('\n');
  const shown = lines.slice(0, 3200);
  const fragment = document.createDocumentFragment();
  for (const [i, line] of shown.entries()) {
    const row = el('div', 'code-row');
    if (diff) {
      row.classList.add('diff-line');
      if (/^(diff --git|index |\+\+\+ |--- )/.test(line)) row.classList.add('meta-line');
      else if (line.startsWith('@@')) row.classList.add('hunk-line');
      else if (line.startsWith('+')) row.classList.add('add-line');
      else if (line.startsWith('-')) row.classList.add('remove-line');
    }
    const number = el('span','line-no', String(i + 1));
    number.setAttribute('aria-hidden','true');
    row.append(number, el('span','line-text',line || ' '));
    fragment.append(row);
  }
  preview.replaceChildren(fragment);
  if (shown.length < lines.length || String(value).length > 180000) {
    $('fileNotice').textContent = '预览已限制显示长度。';
  }
}
function drawEdit() {
  const f = state.file;
  if (!f || state.view !== 'files' || f.kind !== 'text' || !f.revision) return;
  $('editBar').hidden = false;
  const edit = drafts.get(f.path);
  $('editFile').hidden = Boolean(edit);
  $('cancelEdit').hidden = !edit;
  $('saveFile').hidden = !edit;
  $('fileEditArea').hidden = !edit;
  $('preview').hidden = Boolean(edit);
  $('editState').textContent = edit?.message || (edit && edit.content !== edit.base ? '有未保存的更改' : '');
  $('saveFile').disabled = !edit || edit.saving || edit.conflict || edit.content === edit.base;
  $('cancelEdit').disabled = Boolean(edit?.saving);
  if (edit && $('fileEditArea').value !== edit.content) $('fileEditArea').value = edit.content;
}
function renderFile(f) {
  state.file = f;
  $('fileNotice').textContent = ''; $('preview').hidden = false;
  fileHeading(f.kind === 'image' ? '图片预览' : '文件预览', f.path.split('/').pop(), f.path.includes('/') ? f.path : '', prettySize(f.size));
  if (f.kind === 'image') {
    if (!['image/png','image/jpeg','image/gif','image/webp','image/bmp','image/avif','image/x-icon'].includes(f.mime)) {
      $('fileNotice').textContent = '此图片格式暂不支持预览。';
      return;
    }
    const image = el('img','preview-image');
    image.alt = f.path; image.src = 'data:' + f.mime + ';base64,' + f.content;
    image.addEventListener('error', () => { $('fileNotice').textContent = '图片解码失败。'; }, {once:true});
    $('preview').replaceChildren(image);
  } else {
    drawLines(f.content, false); drawEdit();
  }
}
function sameFile(a,b) {
  return a?.kind === b.kind && (a.revision && b.revision ? a.revision === b.revision : a.content === b.content);
}
const conflictMessage = '云端版本已变化。可复制草稿，或放弃编辑后重读文件。';
async function openFile(path) {
  const ticket = ++state.fileVersion;
  const current = state.view === 'files' && state.file?.path === path ? state.file : null;
  const cache = cachedFile(path);
  const edit = drafts.get(path);
  const known = edit?.original || cache || current;
  state.selection = path; viewButtons('files'); remember(key('path'), path);
  if (!current) { resetPreview(); $('preview').hidden = false; }
  if (known) {
    if (!current || !sameFile(current,known)) renderFile(known);
    else if (!edit && !$('fileEditArea').hidden) drawEdit();
    fileSync('显示本地副本 · 正在校验');
  } else {
    fileHeading('文件预览', path.split('/').pop(), path); $('fileNotice').textContent = '正在读取文件…';
  }
  try {
    const reference = cache || known;
    const query = reference?.revision ? {path,ifRevision:reference.revision} : {path};
    let fresh = await rpc('file',query);
    if (ticket !== state.fileVersion) return;
    if (fresh.unchanged) {
      // LRU eviction may happen while validating. The displayed copy is still usable
      // if its path and revision exactly match the server's acknowledgement.
      const local = [cachedFile(path), state.file, drafts.get(path)?.original]
        .find(file => file?.path === path && file.revision === fresh.revision);
      fresh = fresh.path === path && local ? local : await rpc('file',{path});
      if (ticket !== state.fileVersion) return;
    }
    if (fresh.path !== path || typeof fresh.content !== 'string') throw new Error('未能获取完整文件，请重试。');
    cacheFile(fresh); fileSync();
    // Someone may have started editing while the network read was in flight.
    const liveEdit = drafts.get(path);
    if (liveEdit && fresh.revision !== liveEdit.revision) {
      if (!liveEdit.saving) { liveEdit.conflict = true; liveEdit.message = conflictMessage; drawEdit(); }
      return;
    }
    // Keep the actual DOM alive so text selection, scroll, and IME composition survive validation.
    if (!state.file || !sameFile(state.file,fresh)) renderFile(fresh);
  } catch(err) {
    if (ticket !== state.fileVersion) return;
    if (state.file?.path === path) {
      fileSync('使用缓存 · 云端未校验',true);
      $('fileSync').title = asError(err);
    } else $('fileNotice').textContent = asError(err);
  }
}
async function openDiff() {
  const ticket = ++state.fileVersion;
  viewButtons('diff'); resetPreview(); $('preview').hidden = false;
  fileHeading('版本控制', 'Git 差异', state.project || '');
  $('fileNotice').textContent = '正在读取改动…';
  try {
    const data = await rpc('diff');
    if (ticket !== state.fileVersion) return;
    $('fileNotice').textContent = data.diff ? '' : '当前工作区没有 Git 差异。';
    if (data.diff) drawLines(data.diff, true);
    if (data.truncated) $('fileNotice').textContent = '云端差异较长，当前仅显示开头部分。';
  } catch(err) {
    if (ticket === state.fileVersion) $('fileNotice').textContent = asError(err);
  }
}
function beginEdit() {
  const f = state.file;
  if (!f?.revision) return;
  drafts.set(f.path, {original:f, content:f.content, base:f.content, revision:f.revision, saving:false});
  drawEdit(); $('fileEditArea').focus();
}
function cancelEdit() {
  const f = state.file, edit = drafts.get(f?.path);
  if (!edit || edit.saving) return;
  if (edit.content !== edit.base && !window.confirm('放弃此文件尚未保存的编辑，并重新读取云端内容？')) return;
  drafts.delete(f.path); openFile(f.path);
}
async function saveEdit() {
  const f = state.file, edit = drafts.get(f?.path);
  if (!edit || edit.saving || edit.conflict || edit.content === edit.base) return;
  const snapshot = edit.content, scope = state.loadedKey;
  edit.saving = true; edit.message = '正在保存…'; drawEdit();
  try {
    const saved = await rpc('save',{path:f.path,content:snapshot,revision:edit.revision});
    if (scope !== state.loadedKey) return;
    const latest = {...f, ...saved, content:snapshot, size:saved.size ?? new Blob([snapshot]).size};
    edit.revision = latest.revision; edit.base = snapshot; edit.original = latest; edit.message = '';
    cacheFile(latest);
    if (state.file?.path === f.path) {
      // Discard reads initiated before this acknowledged write.
      ++state.fileVersion; state.file = latest; fileSync();
    }
    if (edit.content === snapshot) {
      drafts.delete(f.path);
      if (state.file?.path === f.path) { renderFile(latest); $('editState').textContent = '已保存'; }
    }
  } catch (err) {
    if (scope !== state.loadedKey) return;
    edit.conflict = err.code === 'conflict';
    edit.message = edit.conflict ? conflictMessage : asError(err);
    if (edit.conflict) {
      forgetFile(f.path);
      if (state.file?.path === f.path) openFile(f.path);
    }
  } finally {
    edit.saving = false;
    if (drafts.has(f.path) && state.file?.path === f.path) drawEdit();
  }
}
function treeNodes(entries, ticket) {
  const ul = el('ul','tree-list');
  for (const entry of entries) {
    const li = el('li'), btn = el('button','tree-button');
    const dir = entry.type === 'dir';
    btn.title = entry.path;
    const caret = el('span','tree-caret', dir ? '▸' : '');
    caret.setAttribute('aria-hidden','true');
    const icon = el('i','tree-symbol ' + (dir ? 'dir-symbol' : 'file-symbol'));
    icon.setAttribute('aria-hidden','true');
    btn.append(caret,icon,el('span','tree-name',entry.name)); li.append(btn); ul.append(li);
    if (!dir) {
      btn.dataset.file = entry.path;
      btn.setAttribute('aria-current', String(state.view === 'files' && state.selection === entry.path));
      btn.addEventListener('click', () => openFile(entry.path));
      continue;
    }
    btn.setAttribute('aria-expanded','false');
    let expanded = false, populated = false, busy = false;
    const box = el('div'); box.hidden = true; li.append(box);
    btn.addEventListener('click', async () => {
      expanded = !expanded; box.hidden = !expanded;
      btn.setAttribute('aria-expanded', String(expanded));
      if (!expanded || populated || busy) return;
      busy = true; box.replaceChildren(el('p','tree-message','读取中…'));
      try {
        const data = await rpc('tree',{path:entry.path});
        if (ticket !== state.treeVersion) return;
        box.replaceChildren(data.entries.length ? treeNodes(data.entries,ticket) : el('p','tree-message','空目录'));
        if (data.truncated) box.append(el('p','tree-message','此目录仅显示前 400 项。'));
        populated = true;
      } catch (err) {
        box.replaceChildren(el('p','tree-message',asError(err)));
      } finally { busy = false; }
    });
  }
  return ul;
}
async function loadTree(autoOpen = false) {
  const ticket = ++state.treeVersion;
  $('treeNotice').textContent = '正在读取目录…';
  try {
    const data = await rpc('tree',{path:''});
    if (ticket !== state.treeVersion) return;
    $('tree').replaceChildren(treeNodes(data.entries,ticket));
    $('treeNotice').textContent = data.truncated ? '此目录仅显示前 400 项。' : data.entries.length ? '' : '项目目录为空。';
    if (autoOpen && state.view === 'files') {
      const path = state.selection || data.entries.find(e => e.type === 'file' && e.name.toLowerCase() === 'readme.md')?.path || data.entries.find(e => e.type === 'file')?.path;
      if (path) await openFile(path);
      else $('fileNotice').textContent = '从目录中选择一个文件。';
    }
  } catch(err) {
    if (ticket === state.treeVersion) $('treeNotice').textContent = asError(err);
  }
}

function taskControls() {
  const provider = $('provider').value;
  const caps = state.providers;
  const blocked = !state.ready || state.sending || Boolean(state.pending);
  $('runAgent').disabled = blocked || !caps[provider]?.available || !$('prompt').value.trim();
  $('runCheck').disabled = blocked || !caps.check?.available;
  $('providerHint').textContent = !state.ready ? '等待连接' :
    caps[provider]?.available ? 'Ctrl / ⌘ + Enter' : '云机未提供此助手';
  $('pendingWrap').hidden = !state.pending || state.sending;
  $('retryStart').disabled = state.sending || !state.ready;
}
function taskLabel(task) {
  const label = names[task.provider] || task.provider;
  const prompt = (task.prompt || '').replace(/\s+/g,' ').trim().slice(0,38);
  const stamp = new Date(task.createdAt).getTime();
  const time = Number.isFinite(stamp) ? new Date(stamp).toLocaleTimeString('zh-CN',{hour:'2-digit',minute:'2-digit'}) : '';
  return [label, prompt, labels[task.status] || task.status, time].filter(Boolean).join(' · ');
}
function upsertTask(task) {
  if (!task?.id) return;
  const index = state.tasks.findIndex(item => item.id === task.id);
  if (index < 0) state.tasks.unshift(task); else state.tasks[index] = task;
  if (state.tasks.length > 150) state.tasks.length = 150;
}
function drawHistory() {
  const menu = $('taskHistory');
  const options = state.tasks.map(item => {
    const option = el('option','',taskLabel(item)); option.value = item.id; return option;
  });
  if (!options.length) {
    const option = el('option','','暂无任务'); option.value = ''; options.push(option);
  }
  menu.replaceChildren(...options);
  menu.value = state.selectedTask || '';
  menu.disabled = !state.tasks.length;
}
function drawTaskTitle(task) {
  $('eventHeading').hidden = !task;
  $('taskRequest').hidden = !task?.prompt;
  $('taskRequestText').textContent = task?.prompt || '';
  if (!task) return;
  $('eventProvider').textContent = names[task.provider] || task.provider;
  $('taskStatus').textContent = labels[task.status] || task.status;
  $('taskStatus').dataset.status = task.status;
}
function appendEvent(event) {
  const log = $('eventLog');
  const stick = log.scrollHeight - log.scrollTop - log.clientHeight < 35;
  const text = String(event.text ?? '').slice(0,30000);
  if (!text) return;
  let item = log.lastElementChild;
  if (event.kind === 'delta' && item?.dataset.kind === 'delta' && item.textContent.length + text.length <= 30000) {
    item.append(document.createTextNode(text));
  } else {
    item = el('div','event',text); item.dataset.kind = event.kind; log.append(item);
  }
  state.eventsSize += text.length;
  while ((state.eventsSize > 180000 || log.childElementCount > 400) && log.childElementCount > 1) {
    const first = log.firstElementChild; state.eventsSize -= first.textContent.length; first.remove();
  }
  if (stick) log.scrollTop = log.scrollHeight;
}
function scheduleEvents(ticket,delay) {
  clearTimeout(state.timer);
  state.timer = setTimeout(() => pollEvents(ticket),delay);
}
async function pollEvents(ticket) {
  const taskId = state.selectedTask;
  if (!taskId || ticket !== state.taskVersion) return;
  try {
    const result = await rpc('events',{taskId,after:state.cursor});
    if (ticket !== state.taskVersion || taskId !== state.selectedTask) return;
    for (const event of result.events) {
      if (event.seq <= state.cursor) continue;
      appendEvent(event); state.cursor = event.seq;
    }
    const more = result.hasMore === true;
    const task = result.task;
    state.retryCount = 0;
    upsertTask(task); drawTaskTitle(task); drawHistory();
    $('taskNotice').textContent = !state.cursor && !isActive(task) ? '此任务没有输出。' : '';
    if (more) scheduleEvents(ticket,0);
    else if (isActive(task)) scheduleEvents(ticket,1000);
  } catch(err) {
    if (ticket !== state.taskVersion) return;
    state.retryCount++;
    if (['not_found','task_not_found','invalid_request'].includes(err.code)) {
      $('taskNotice').textContent = asError(err);
    } else {
      $('taskNotice').textContent = asError(err) + ' 正在重连…';
      scheduleEvents(ticket,Math.min(12000,800 * (2 ** Math.min(4,state.retryCount))));
    }
  }
}
function selectTask(id) {
  const next = ++state.taskVersion;
  clearTimeout(state.timer);
  state.selectedTask = id || null; state.cursor = 0; state.eventsSize = 0; state.retryCount = 0;
  remember(key('task'),state.selectedTask);
  $('eventLog').replaceChildren(); $('taskRequest').open = false;
  const item = state.tasks.find(t => t.id === id);
  drawTaskTitle(item); drawHistory();
  $('taskNotice').textContent = id ? '正在读取任务输出…' : '运行一次项目检查或编码任务后，可在这里查看输出。';
  if (id) pollEvents(next);
}
async function loadTasks() {
  const ticket = ++state.taskListVersion;
  const selectionVersion = state.taskVersion;
  try {
    const data = await rpc('tasks');
    if (ticket !== state.taskListVersion || selectionVersion !== state.taskVersion) return;
    state.tasks = data.tasks.slice(0,150);
    const id = state.selectedTask || stored(key('task'));
    const selected = state.tasks.find(t => t.id === id);
    if (selected && state.selectedTask === id) { drawHistory(); drawTaskTitle(selected); return; }
    selectTask(selected?.id || state.tasks[0]?.id || '');
  } catch(err) {
    if (ticket === state.taskListVersion && selectionVersion === state.taskVersion) $('taskNotice').textContent = asError(err);
  }
}
function rememberPending(value) {
  state.pending = value;
  remember(key('pending'),value ? JSON.stringify(value) : null);
}
async function submitStart(request) {
  if (state.sending || !request) return;
  const scope = state.loadedKey;
  state.sending = true; rememberPending(request); taskControls();
  $('startNotice').textContent = '正在提交到云端…';
  try {
    const item = await rpc('start',request);
    if (state.loadedKey !== scope) return;
    ++state.taskListVersion;
    upsertTask(item); rememberPending(null);
    if (request.provider !== 'check' && $('prompt').value.trim() === request.prompt) {
      $('prompt').value = ''; remember(key('prompt'),null);
    }
    $('startNotice').textContent = '';
    selectTask(item.id);
  } catch(err) {
    if (state.loadedKey !== scope) return;
    if (['invalid_request','invalid_argument','bad_request','unknown_provider','unavailable','provider_unavailable','invalid_prompt'].includes(err.code)) {
      rememberPending(null); $('startNotice').textContent = asError(err);
    } else {
      $('pendingMessage').textContent = asError(err) + ' 重试将沿用此次请求编号。';
      $('startNotice').textContent = '';
    }
  } finally {
    if (state.loadedKey === scope) { state.sending = false; taskControls(); }
  }
}
function run(provider) {
  if (state.pending || state.sending || !state.ready || !state.providers[provider]?.available) return;
  const prompt = provider === 'check' ? '' : $('prompt').value.trim();
  if (provider !== 'check' && !prompt) return;
  submitStart({requestId:crypto.randomUUID(),provider,prompt});
}
function restore(status) {
  const projectName = String(status.project || '项目');
  const scope = [location.pathname,status.mode || '',status.workspaceId || status.machineId || '',projectName].join('/');
  state.mode = status.mode; connection('online');
  state.project = projectName; state.providers = status.providers || {};
  $('topProject').textContent = projectName; $('branch').textContent = status.branch || '未获取分支';
  if (state.loadedKey === scope) return;
  if (state.loadedKey) {
    ++state.taskVersion; ++state.fileVersion; ++state.taskListVersion;
    clearTimeout(state.timer); state.tasks = []; state.selectedTask = null;
    state.pending = null; state.sending = false; drafts.clear(); fileCache.clear(); cachedBytes = 0;
    state.view = 'files'; state.file = null; $('eventLog').replaceChildren();
    $('fileNotice').textContent = ''; $('startNotice').textContent = '';
  }
  state.loadedKey = scope; state.selection = stored(key('path')) || null;
  const provider = stored(key('provider'));
  if (provider === 'codex' || provider === 'claude') $('provider').value = provider;
  else if (!state.providers.codex?.available && state.providers.claude?.available) $('provider').value = 'claude';
  $('prompt').value = stored(key('prompt'));
  try {
    const pending = JSON.parse(stored(key('pending'),'null'));
    state.pending = pending && typeof pending.requestId === 'string' && Object.hasOwn(names,pending.provider) ? pending : null;
  } catch { state.pending = null; }
  if (state.pending) $('pendingMessage').textContent = '上次提交尚未确认。可以用原请求编号再次提交。';
}
async function refresh() {
  const version = ++state.refreshVersion;
  connection('loading');
  try {
    const status = await rpc('status');
    if (version !== state.refreshVersion) return;
    restore(status); state.ready = true; taskControls();
    await Promise.all([loadTree(state.view === 'files'),loadTasks(),state.view === 'diff' ? openDiff() : Promise.resolve()]);
  } catch(err) {
    if (version !== state.refreshVersion) return;
    connection('error'); state.ready = false; taskControls();
    $('treeNotice').textContent = asError(err); $('taskNotice').textContent = '刷新工作区可重新连接。';
    if (!state.file) $('fileNotice').textContent = asError(err);
  }
}

$('mobileFiles').addEventListener('click', () => mobilePage('files'));
$('mobileTasks').addEventListener('click', () => mobilePage('tasks'));
$('refresh').addEventListener('click',refresh);
$('reloadTree').addEventListener('click',() => loadTree());
$('reloadTasks').addEventListener('click',loadTasks);
$('showDiff').addEventListener('click',openDiff);
$('showFiles').addEventListener('click',() => { viewButtons('files'); if (state.selection) openFile(state.selection); else loadTree(true); });
$('editFile').addEventListener('click',beginEdit);
$('cancelEdit').addEventListener('click',cancelEdit);
$('saveFile').addEventListener('click',saveEdit);
$('fileEditArea').addEventListener('input',() => {
  const edit = drafts.get(state.file?.path);
  if (!edit) return;
  edit.content = $('fileEditArea').value;
  if (!edit.conflict && !edit.saving) edit.message = '';
  drawEdit();
});
$('fileEditArea').addEventListener('keydown',event => {
  if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === 's') { event.preventDefault(); saveEdit(); }
});
$('taskHistory').addEventListener('change',() => selectTask($('taskHistory').value));
$('provider').addEventListener('change',() => { remember(key('provider'),$('provider').value); taskControls(); });
$('prompt').addEventListener('input',() => { if (state.loadedKey) remember(key('prompt'),$('prompt').value); taskControls(); });
$('prompt').addEventListener('keydown',event => {
  if (!event.isComposing && (event.ctrlKey || event.metaKey) && event.key === 'Enter') { event.preventDefault(); run($('provider').value); }
});
$('runAgent').addEventListener('click',() => run($('provider').value));
$('runCheck').addEventListener('click',() => run('check'));
$('retryStart').addEventListener('click',() => submitStart(state.pending));
$('dismissPending').addEventListener('click',() => {
  if (!state.pending || state.sending) return;
  if (!window.confirm('云端可能已经收到任务。清除本地重试记录后，可从任务记录查看云端结果。继续吗？')) return;
  rememberPending(null); taskControls(); loadTasks();
});
window.addEventListener('offline',() => connection('error'));
window.addEventListener('online',refresh);
window.addEventListener('beforeunload',event => {
  for (const draft of drafts.values()) if (draft.content !== draft.base) { event.preventDefault(); event.returnValue = ''; break; }
});
refresh();

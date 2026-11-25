'use strict';

const $ = id => document.getElementById(id);
const names = { check: '项目检查', codex: 'Codex', claude: 'Claude Code', proposal: 'Codex · 修改建议' };
const labels = { queued: '排队中', running: '运行中', cancelling: '正在取消', cancelled: '已取消', succeeded: '已完成', failed: '失败' };
const isActive = task => ['queued', 'running', 'cancelling'].includes(task?.status);
const cancelRequests = new Map();
const drafts = new Map();
const unbackedDrafts = new Map();
// sessionStorage belongs to this tab. A launcher can serve different projects
// at the same origin over its lifetime, so also scope every entry by workspace.
function draftPrefix() { return 'cloudwork.fileDraft.v1.' + JSON.stringify(state.fileScope) + '.'; }
function workspaceScope(status) {
  return [location.pathname,status.mode || '',status.workspaceId || status.machineId || '',status.project || '项目'].join('/');
}
function persistDraft(path, edit) {
  try {
    const key = draftPrefix() + path;
    if (!edit || (edit.content === edit.base && edit.submitted === undefined)) sessionStorage.removeItem(key);
    else sessionStorage.setItem(key, JSON.stringify({
      base:edit.base, revision:edit.revision, content:edit.content,
      conflict:Boolean(edit.conflict), submitted:edit.submitted,
    }));
    if (edit) edit.storageFailed = false;
  } catch {
    if (edit) edit.storageFailed = true;
  }
}
function restoreFileScope(scope) {
  if (state.fileScope === scope) return;
  for (const [path, edit] of drafts) {
    if (edit.storageFailed && (edit.content !== edit.base || edit.submitted !== undefined)) {
      if (!unbackedDrafts.has(state.fileScope)) unbackedDrafts.set(state.fileScope,new Map());
      unbackedDrafts.get(state.fileScope).set(path,edit);
    }
  }
  ++state.fileVersion; ++state.treeVersion;
  drafts.clear(); fileCache.clear(); cachedBytes = 0;
  state.fileScope = scope;
  state.selection = stored(scope + '.path') || null;
  viewButtons('files'); resetPreview(); clearTree();
  $('fileEditArea').value = ''; fileHeading('文件预览','项目文件');
  const prefix = draftPrefix();
  try {
    state.selection = sessionStorage.getItem('cloudwork.selectedFile.' + scope) || state.selection;
    for (let i = 0; i < sessionStorage.length; i++) {
      const key = sessionStorage.key(i);
      if (!key?.startsWith(prefix)) continue;
      const path = key.slice(prefix.length);
      let item;
      try { item = JSON.parse(sessionStorage.getItem(key)); } catch { continue; }
      if (!path || !item || typeof item.content !== 'string' || typeof item.base !== 'string' || typeof item.revision !== 'string' ||
          (item.content === item.base && typeof item.submitted !== 'string') || !/^[0-9a-f]{64}$/.test(item.revision)) continue;
      const original = {path,kind:'text',revision:item.revision,content:item.base,size:new Blob([item.base]).size};
      drafts.set(path, {original, base:item.base, content:item.content, revision:item.revision,
        submitted:typeof item.submitted === 'string' ? item.submitted : undefined,
        conflict:item.conflict === true, message:item.conflict === true ? conflictMessage : '已恢复此标签页的未保存草稿。'});
    }
  } catch { /* Typing and saving still work when tab storage is disabled. */ }
  for (const [path, edit] of unbackedDrafts.get(scope) || []) drafts.set(path,edit);
  unbackedDrafts.delete(scope);
  drawDraftMenu();
}
function drawDraftMenu() {
  const menu = $('fileDrafts');
  const options = [];
  for (const [path, edit] of drafts) {
    if ((edit.content !== edit.base || edit.submitted !== undefined) && (state.view !== 'files' || state.selection !== path)) {
      const option = el('option','',displayPath(path));
      option.value = path;
      options.push(option);
    }
  }
  menu.hidden = !options.length;
  const label = el('option','','打开其他未保存草稿');
  label.value = '';
  menu.replaceChildren(label, ...options);
}
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
  taskControls();
}
const state = {
  project: null, workspaceId: null, providers: {}, gitIssue: '', diffScopes: false, ready: false, view: 'files', file: null,
  fileScope: null, fileVersion: 0, treeVersion: 0, refreshVersion: 0, taskListVersion: 0, taskVersion: 0,
  tasks: [], historyAnchors: [], historyNext: null, historyTarget: null,
  selectedTask: null, selectedRecord: null, timer: null,
  selection: null, sending: false, pending: null, feedbackParent: null, taskNavigation: 0, retryCount: 0, loadedKey: null, workspaceVersion: 0,
};
function el(tag, cls, content) {
  const node = document.createElement(tag);
  if (cls) node.className = cls;
  if (content !== undefined) node.textContent = content;
  return node;
}
function stored(key, fallback = '', storage = 'localStorage') {
  try { return window[storage].getItem('cloudwork.' + key) ?? fallback; }
  catch { return fallback; }
}
function remember(key, value, storage = 'localStorage') {
  try {
    if (value === null) window[storage].removeItem('cloudwork.' + key);
    else window[storage].setItem('cloudwork.' + key, value);
  } catch { /* Storage can be unavailable in private browsing. */ }
}
function key(suffix) { return state.loadedKey + '.' + suffix; }
function connection(mode) {
  $('connection').dataset.state = mode;
  $('connectionLabel').textContent = {loading:'连接中', online:state.mode === 'local' ? '本机模式' : '云机已连接', error:'连接中断'}[mode];
}
async function rpc(op, fields = {}, isCurrent = () => true) {
  let res, envelope;
  const workspace = state.workspaceId;
  const reportConnection = () => workspace === state.workspaceId && isCurrent();
  const headers = {'Content-Type':'application/json'};
  if (op !== 'status' && state.workspaceId) headers['X-Workspace-Id'] = state.workspaceId;
  try {
    res = await fetch('/api/rpc', {
      method:'POST', headers,
      credentials:'same-origin', cache:'no-store', body:JSON.stringify({op, ...fields}),
      signal:AbortSignal.timeout(90000),
    });
    envelope = await res.json();
  } catch {
    if (op !== 'status' && reportConnection()) connection('error');
    const err = new Error('与工作区的连接中断。恢复网络后可重试。');
    err.code = 'transport';
    throw err;
  }
  if (!res.ok || !envelope.ok) {
    const err = new Error(envelope.error?.message || '工作区请求失败');
    err.code = envelope.error?.code || 'transport';
    if (reportConnection() && err.code === 'workspace_changed') {
      state.ready = false;
      taskControls();
      connection('error');
      $('connectionLabel').textContent = '项目已切换，请刷新';
    }
    if (op !== 'status' && reportConnection() && (res.status >= 500 || err.code === 'transport')) connection('error');
    throw err;
  }
  if (op !== 'status' && reportConnection() && state.ready) connection('online');
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
  drawDraftMenu();
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
function displayPath(path) {
  // Python's filesystem decoder preserves non-UTF-8 bytes as lone low surrogates.
  return path.replaceAll('\\','\\\\').replace(/[\uDC80-\uDCFF]/gu, ch => '\\x' + (ch.charCodeAt(0) - 0xDC00).toString(16));
}
function fileHeading(label, title, path = '', size = '') {
  $('viewLabel').textContent = label;
  $('editorTab').textContent = $('fileTitle').textContent = displayPath(title);
  $('filePath').textContent = displayPath(path);
  $('fileSize').textContent = size;
}
function resetPreview() {
  $('preview').replaceChildren(); $('fileNotice').textContent = '';
  $('editBar').hidden = true; $('fileEditArea').hidden = true;
  state.file = null; fileSync();
}
function drawRows(preview, rows, diff, offset = 0) {
  const fragment = document.createDocumentFragment();
  for (const [i, line] of rows.entries()) {
    const row = el('div', 'code-row');
    if (diff) {
      row.classList.add('diff-line');
      if (/^(diff --git|index |\+\+\+ |--- )/.test(line)) row.classList.add('meta-line');
      else if (line.startsWith('@@')) row.classList.add('hunk-line');
      else if (line.startsWith('+')) row.classList.add('add-line');
      else if (line.startsWith('-')) row.classList.add('remove-line');
    }
    const number = el('span','line-no', String(offset + i + 1));
    number.setAttribute('aria-hidden','true');
    row.append(number, el('span','line-text',line || ' '));
    fragment.append(row);
  }
  preview.replaceChildren(fragment);
}
function drawFileLines(value) {
  const lines = value.slice(0,180000).split('\n'), shown = lines.slice(0,3200);
  drawRows($('preview'),shown,false);
  return shown.length < lines.length || value.length > 180000;
}
function textareaText(raw) { return raw.replace(/\r\n?/g,'\n'); }
function editedFileText(edit, value, caret) {
  const raw = edit.content;
  if (edit.lineEnding === undefined) {
    const first = raw.match(/\r\n|\r|\n/)?.[0] || edit.base.match(/\r\n|\r|\n/)?.[0] || '\n';
    edit.lineEnding = /[\r\n]/.test(raw.replaceAll(first,'')) ? null : first;
  }
  if (edit.lineEnding) return edit.lineEnding === '\n' ? value : value.replaceAll('\n',edit.lineEnding);
  const previous = textareaText(raw);
  if (value === previous) return raw;
  if (value === textareaText(edit.base)) return edit.base;

  // Native textarea offsets count UTF-16 units and treat every line break as one.
  // Match the suffix after the caret first so edits beside repeated empty lines
  // keep the original endings on the correct side of the insertion/deletion.
  let oldEnd = previous.length, newEnd = value.length;
  while (oldEnd > 0 && newEnd > caret && previous[oldEnd - 1] === value[newEnd - 1]) {
    oldEnd--; newEnd--;
  }
  let start = 0;
  while (start < oldEnd && start < newEnd && previous[start] === value[start]) start++;
  while (oldEnd > start && newEnd > start && previous[oldEnd - 1] === value[newEnd - 1]) {
    oldEnd--; newEnd--;
  }

  let rawStart = start, rawEnd = oldEnd, extra = 0, preceding = '', following = '';
  const replacedBreaks = [];
  for (const match of raw.matchAll(/\r\n|\r|\n/g)) {
    const ending = match[0], offset = match.index - extra;
    if (offset >= oldEnd) { following = ending; break; }
    extra += ending.length - 1;
    rawEnd += ending.length - 1;
    if (offset < start) { rawStart += ending.length - 1; preceding = ending; }
    else replacedBreaks.push(ending);
  }
  const convention = replacedBreaks.at(-1) || following || preceding || edit.base.match(/\r\n|\r|\n/)?.[0] || '\n';
  const parts = [];
  function append(part) {
    if (!part) return;
    // A CR and LF that were separate breaks before the edit may now touch.
    if (parts.at(-1)?.endsWith('\r') && part.startsWith('\n')) parts.push('\r');
    parts.push(part);
  }
  append(raw.slice(0,rawStart));
  const lines = value.slice(start,newEnd).split('\n');
  for (const [i, line] of lines.entries()) {
    if (i) append(replacedBreaks[i - 1] || convention);
    append(line);
  }
  append(raw.slice(rawEnd));
  return parts.join('');
}
function drawEdit() {
  taskControls();
  const f = state.file;
  if (!f || state.view !== 'files' || f.kind !== 'text' || !f.revision) return;
  $('editBar').hidden = false;
  const edit = drafts.get(f.path);
  $('editFile').hidden = Boolean(edit);
  $('cancelEdit').hidden = !edit;
  $('saveFile').hidden = !edit;
  $('fileEditArea').hidden = !edit;
  $('preview').hidden = Boolean(edit);
  const message = edit?.message || (edit && edit.content !== edit.base ? '有未保存的更改' : '');
  $('editState').textContent = edit?.storageFailed ? message + ' 浏览器无法备份最新草稿，请在关闭或刷新前保存。' : message;
  $('saveFile').disabled = !edit || !state.ready || edit.saving || edit.conflict || edit.content === edit.base;
  $('cancelEdit').disabled = Boolean(edit?.saving);
  if (edit) {
    const value = textareaText(edit.content);
    if ($('fileEditArea').value !== value) $('fileEditArea').value = value;
  }
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
    image.alt = displayPath(f.path); image.src = 'data:' + f.mime + ';base64,' + f.content;
    image.addEventListener('error', () => { $('fileNotice').textContent = '图片解码失败。'; }, {once:true});
    $('preview').replaceChildren(image);
  } else {
    if (drawFileLines(textareaText(f.content))) $('fileNotice').textContent = '预览已限制显示长度。';
    drawEdit();
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
  state.selection = path; viewButtons('files'); remember(state.fileScope + '.path', path);
  try { sessionStorage.setItem('cloudwork.selectedFile.' + state.fileScope, path); } catch { /* optional */ }
  if (!current) { resetPreview(); $('preview').hidden = false; }
  if (known) {
    if (!current || !sameFile(current,known)) renderFile(known);
    else if (!edit && !$('fileEditArea').hidden) drawEdit();
    fileSync('显示本地副本 · 正在校验');
  } else {
    fileHeading('文件预览', path.split('/').pop(), path); $('fileNotice').textContent = '正在读取文件…';
  }
  if (!state.ready) {
    if (known) fileSync('使用本地副本 · 云端未校验',true);
    else $('fileNotice').textContent = '重新连接后可读取此文件。';
    return;
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
      if (!liveEdit.saving && fresh.kind === 'text' && fresh.content === liveEdit.content) {
        // A save may have reached the workspace just before a reload or a lost acknowledgement.
        drafts.delete(path); persistDraft(path,null); drawDraftMenu(); renderFile(fresh);
        $('editState').textContent = '草稿与云端内容一致。';
      } else if (!liveEdit.saving && fresh.kind === 'text' && fresh.content === liveEdit.submitted) {
        // Recover the confirmed base of an interrupted save without losing subsequent typing.
        Object.assign(liveEdit, {original:fresh,base:fresh.content,revision:fresh.revision,
          submitted:undefined,conflict:false,message:'上次保存已确认，之后的编辑尚未保存。'});
        persistDraft(path,liveEdit); renderFile(fresh);
      } else if (!liveEdit.saving) {
        liveEdit.conflict = true; liveEdit.message = conflictMessage; persistDraft(path,liveEdit); drawEdit();
      }
      return;
    }
    if (liveEdit?.conflict) {
      liveEdit.conflict = false; liveEdit.message = ''; persistDraft(path,liveEdit); drawEdit();
    }
    // Keep the actual DOM alive so text selection, scroll, and IME composition survive validation.
    if (!state.file || !sameFile(state.file,fresh)) renderFile(fresh);
  } catch(err) {
    if (ticket !== state.fileVersion) return;
    if (state.file?.path === path) {
      const edit = drafts.get(path);
      if (edit && ['not_found','invalid_path','unsupported_file','too_large'].includes(err.code)) {
        edit.conflict = true; edit.message = asError(err) + ' 可复制本地草稿。';
        persistDraft(path,edit); drawEdit();
      }
      fileSync('使用缓存 · 云端未校验',true);
      $('fileSync').title = asError(err);
    } else $('fileNotice').textContent = asError(err);
  }
}
function updateGitStatus({gitState,gitMessage,branch}) {
  const label = $('branch');
  if (gitState === 'none' || gitState === 'error') {
    label.textContent = gitState === 'none' ? '项目根目录没有 Git 仓库' : 'Git 仓库不可用';
    state.gitIssue = gitMessage || label.textContent + '。';
  } else {
    label.textContent = branch || '未获取分支';
    state.gitIssue = '';
  }
  label.title = state.gitIssue || label.textContent;
}
async function recheckDiffRepository(current) {
  try {
    const status = await rpc('status',{},current);
    if (!current() || workspaceScope(status) !== state.loadedKey) return;
    updateGitStatus(status);
    if (status.providers?.check) state.providers.check = status.providers.check;
    taskControls();
    return state.gitIssue;
  } catch { /* Keep the diff result and last confirmed repository status. */ }
}
const diffLabels = {staged:'已暂存', unstaged:'未暂存（含未跟踪）', combined:'合并差异'};
function makeDiffLayer(scope, onRetry) {
  const box = el('details','diff-layer'), heading = el('summary','',diffLabels[scope]);
  box.open = true; box.dataset.diffScope = scope;
  const lines = el('div','diff-lines'), notice = el('p','diff-notice','正在读取改动…');
  notice.setAttribute('role','status');
  lines.setAttribute('role','region'); lines.setAttribute('aria-label',diffLabels[scope] + '差异正文');
  const retry = el('button','subtle-button','重试读取');
  retry.setAttribute('aria-label','重试读取' + diffLabels[scope] + '差异');
  retry.hidden = true; retry.addEventListener('click',onRetry);
  box.append(heading,notice,retry,lines);
  return {box,lines,notice,retry,empty:false};
}
const diffPageSize = 800;
function drawDiffPages(layer, scope, text) {
  const rows = text.split('\n');
  if (rows.at(-1) === '') rows.pop();
  if (rows.length <= diffPageSize) { drawRows(layer.lines,rows,true); return; }
  const total = Math.ceil(rows.length / diffPageSize);
  const nav = el('nav','diff-pages'), select = el('select','diff-page');
  nav.setAttribute('aria-label',diffLabels[scope] + '差异分页');
  select.setAttribute('aria-label',diffLabels[scope] + '差异页码');
  const prev = el('button','diff-prev','上一页'), next = el('button','diff-next','下一页');
  for (let page = 1; page <= total; page++) {
    const first = (page - 1) * diffPageSize + 1, last = Math.min(page * diffPageSize,rows.length);
    const option = el('option','','第 ' + page + ' 页 · ' + first + '–' + last + ' 行');
    option.value = String(page); select.append(option);
  }
  function show(page) {
    const offset = (page - 1) * diffPageSize;
    drawRows(layer.lines,rows.slice(offset,offset + diffPageSize),true,offset);
    layer.lines.scrollTop = layer.lines.scrollLeft = 0;
    select.value = String(page);
    const from = document.activeElement;
    prev.disabled = page === 1; next.disabled = page === total;
    if ((from === prev && prev.disabled) || (from === next && next.disabled)) select.focus({preventScroll:true});
  }
  select.addEventListener('change',() => show(Number(select.value)));
  prev.addEventListener('click',() => show(Number(select.value) - 1));
  next.addEventListener('click',() => show(Number(select.value) + 1));
  nav.append(prev,select,next); layer.lines.before(nav); show(1);
}
function renderDiffLayer(layer, scope, data) {
  if (data.diff) { drawDiffPages(layer,scope,data.diff); layer.lines.tabIndex = 0; }
  layer.empty = !data.diff && !data.truncated;
  const empty = {staged:'没有已暂存的改动。', unstaged:'没有未暂存或未跟踪的改动。'};
  layer.notice.textContent = data.truncated ? '服务端已截断此差异，当前仅显示开头部分。' :
    layer.empty ? empty[scope] || '' : '';
}
async function openDiff() {
  const ticket = ++state.fileVersion;
  const current = () => ticket === state.fileVersion;
  const preview = $('preview'), notice = $('fileNotice');
  viewButtons('diff'); resetPreview(); preview.hidden = false;
  fileHeading('版本控制', 'Git 差异', state.project || '');
  const layers = new Map();
  let settled = false, statusCheck = null;
  const accepting = () => current() && !settled;
  const checkRepository = () => statusCheck ??= recheckDiffRepository(settled ? current : accepting);
  function addLayers(scopes) {
    layers.clear(); preview.replaceChildren(); notice.textContent = '';
    for (const scope of scopes) {
      const layer = makeDiffLayer(scope,() => { statusCheck = null; read(scope); });
      layers.set(scope,layer); preview.append(layer.box);
    }
  }
  function unavailable(message) {
    settled = true; layers.clear(); preview.replaceChildren(); notice.textContent = message;
  }
  async function showEmpty() {
    const applies = settled ? current : accepting;
    let issue = '';
    if (state.gitIssue) issue = await checkRepository() ?? '差异结果为空，暂时无法核对 Git 仓库状态。';
    if (applies() && layers.size && [...layers.values()].every(layer => layer.empty)) {
      notice.textContent = issue || '当前工作区没有 Git 差异。';
    }
  }
  async function read(scope) {
    const pending = layers.get(scope);
    if (pending) { pending.retry.hidden = true; pending.notice.textContent = '正在读取改动…'; }
    try {
      const data = await rpc('diff',{scope},accepting);
      if (!accepting()) return;
      if (data.gitState === 'none' || data.gitState === 'error') {
        updateGitStatus(data); taskControls(); unavailable(state.gitIssue); return;
      }
      if (typeof data.diff !== 'string' || (Object.hasOwn(data,'scope') && data.scope !== scope)) {
        throw new Error('未能读取此范围的 Git 差异，请重试。');
      }
      if (!Object.hasOwn(data,'scope')) {
        // Older workers accept but ignore scope. Only the first combined reply is shown.
        settled = true; statusCheck = null; state.diffScopes = false;
        addLayers(['combined']); renderDiffLayer(layers.get('combined'),'combined',data);
        await showEmpty();
        return;
      }
      const probe = !layers.size;
      if (probe) { state.diffScopes = true; addLayers(['staged','unstaged']); }
      renderDiffLayer(layers.get(scope),scope,data);
      await Promise.all([showEmpty(),probe ? read('unstaged') : null]);
    } catch(err) {
      if (!accepting()) return;
      const layer = layers.get(scope);
      if (layer) { layer.notice.textContent = asError(err); layer.retry.hidden = false; }
      else notice.textContent = asError(err);
      if (err.code === 'git_error') {
        const issue = await checkRepository();
        if (accepting() && issue) unavailable(issue);
      }
    }
  }
  if (state.diffScopes) {
    addLayers(['staged','unstaged']);
    await Promise.all([read('staged'),read('unstaged')]);
  } else {
    notice.textContent = '正在读取改动…';
    await read('staged');
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
  drafts.delete(f.path); persistDraft(f.path,null); openFile(f.path);
}
async function saveEdit() {
  const f = state.file, edit = drafts.get(f?.path);
  if (!edit || !state.ready || edit.saving || edit.conflict || edit.content === edit.base) return;
  const snapshot = edit.content, scope = state.fileScope;
  edit.submitted = snapshot; persistDraft(f.path,edit);
  edit.saving = true; edit.message = '正在保存…'; drawEdit();
  try {
    const saved = await rpc('save',{path:f.path,content:snapshot,revision:edit.revision});
    if (scope !== state.fileScope || drafts.get(f.path) !== edit) return;
    const latest = {...f, ...saved, content:snapshot, size:saved.size ?? new Blob([snapshot]).size};
    edit.revision = latest.revision; edit.base = snapshot; edit.original = latest; edit.message = '';
    edit.submitted = undefined;
    cacheFile(latest);
    if (state.file?.path === f.path) {
      // Discard reads initiated before this acknowledged write.
      ++state.fileVersion; state.file = latest; fileSync();
    }
    if (edit.content === snapshot) {
      drafts.delete(f.path); persistDraft(f.path,null); drawDraftMenu();
      if (state.file?.path === f.path) { renderFile(latest); $('editState').textContent = '已保存'; }
    } else persistDraft(f.path,edit);
  } catch (err) {
    if (scope !== state.fileScope || drafts.get(f.path) !== edit) return;
    edit.conflict = err.code === 'conflict';
    edit.message = edit.conflict ? conflictMessage : asError(err);
    if (edit.conflict) {
      persistDraft(f.path,edit); forgetFile(f.path);
      if (state.file?.path === f.path) openFile(f.path);
    }
  } finally {
    edit.saving = false;
    if (drafts.has(f.path) && state.file?.path === f.path) drawEdit();
  }
}
let rootDirectory = null;
const directoryReads = new Map();
function drawDirectory(directory) {
  const busy = directoryReads.has(directory);
  directory.list.setAttribute('aria-busy',String(busy));
  directory.notice.textContent = busy ? '正在读取目录…' : directory.message;
  directory.notice.hidden = !directory.notice.textContent;
  const action = directory.refreshRequired ? '刷新目录' : directory.retry ? '重试读取' : directory.cursor ? '加载更多' : '';
  directory.more.hidden = !action;
  directory.more.textContent = action;
  // Keep keyboard focus on the paging control while a cloud read is outstanding.
  directory.more.setAttribute('aria-disabled',String(busy));
}
function invalidateDirectoryReads(box) {
  for (const directory of directoryReads.keys()) {
    if (box.contains(directory.box)) {
      directoryReads.delete(directory);
      if (!directory.loaded) directory.retry = true;
      drawDirectory(directory);
    }
  }
}
function clearTree() {
  invalidateDirectoryReads($('tree'));
  rootDirectory = null; $('tree').replaceChildren(); $('treeNotice').textContent = '';
}
function makeDirectory(path) {
  const box = el('div','tree-directory'), list = el('ul','tree-list');
  const notice = el('p','tree-message'), more = el('button','tree-more');
  box.dataset.directory = path; box.setAttribute('role','group'); box.setAttribute('aria-label',displayPath(path) || '项目根目录');
  notice.dataset.treeNotice = path; notice.setAttribute('role','status');
  more.dataset.treeMore = path;
  box.append(list,more,notice);
  const directory = {path,box,list,notice,more,cursor:null,loaded:false,retry:false,refreshRequired:false,message:''};
  more.addEventListener('click',() => {
    if (!directoryReads.has(directory)) readDirectory(directory,directory.refreshRequired || !directory.loaded);
  });
  drawDirectory(directory);
  return directory;
}
function treeNodes(entries) {
  const fragment = document.createDocumentFragment();
  for (const entry of entries) {
    const li = el('li'), btn = el('button','tree-button');
    const dir = entry.type === 'dir';
    btn.title = displayPath(entry.path);
    const caret = el('span','tree-caret', dir ? '▸' : '');
    caret.setAttribute('aria-hidden','true');
    const icon = el('i','tree-symbol ' + (dir ? 'dir-symbol' : 'file-symbol'));
    icon.setAttribute('aria-hidden','true');
    btn.append(caret,icon,el('span','tree-name',displayPath(entry.name))); li.append(btn); fragment.append(li);
    if (!dir) {
      btn.dataset.file = entry.path;
      btn.setAttribute('aria-current', String(state.view === 'files' && state.selection === entry.path));
      btn.addEventListener('click', () => openFile(entry.path));
      continue;
    }
    btn.dataset.folder = entry.path; btn.setAttribute('aria-expanded','false');
    let child = null;
    btn.addEventListener('click', () => {
      const expanded = btn.getAttribute('aria-expanded') !== 'true';
      btn.setAttribute('aria-expanded', String(expanded));
      if (!child) { child = makeDirectory(entry.path); li.append(child.box); }
      child.box.hidden = !expanded;
      if (!expanded) invalidateDirectoryReads(child.box);
      else if (!child.loaded) readDirectory(child,true);
    });
  }
  return fragment;
}
async function readDirectory(directory, reset) {
  const {path,box,list,more} = directory;
  if (reset) invalidateDirectoryReads(box);
  const cursor = reset ? null : directory.cursor;
  const generation = state.treeVersion, request = {};
  directoryReads.set(directory,request); drawDirectory(directory);
  const current = () => generation === state.treeVersion && directoryReads.get(directory) === request && box.isConnected;
  try {
    const data = await rpc('tree',cursor ? {path,cursor} : {path},current);
    if (!current()) return;
    if (data.path !== path || !Array.isArray(data.entries)) throw new Error('未能读取此目录，请重试。');
    const legacy = !Object.hasOwn(data,'nextCursor');
    const focused = document.activeElement === more;
    const scroll = box.closest('.tree-column'), top = scroll.scrollTop;
    // An older worker ignores the cursor and sends its first page again.
    const nodes = treeNodes(cursor && legacy ? [] : data.entries);
    const first = nodes.firstElementChild?.firstElementChild;
    if (reset) { invalidateDirectoryReads(list); list.replaceChildren(nodes); }
    else list.append(nodes);
    directory.cursor = data.nextCursor || null;
    directory.loaded = true; directory.retry = false; directory.refreshRequired = Boolean(cursor && legacy);
    directory.message = directory.refreshRequired ? '当前工作进程不支持继续翻页，可刷新目录重读。' :
      legacy && data.truncated ? '此目录仅显示前 400 项。' :
      list.childElementCount ? '' : path ? '空目录' : '项目目录为空。';
    if (focused) {
      const target = first || list.lastElementChild?.firstElementChild;
      if (target) {
        target.focus({preventScroll:true});
        if (reset) target.scrollIntoView({block:'nearest'});
        else scroll.scrollTop = top;
      }
    }
    return data;
  } catch(err) {
    if (!current()) return;
    directory.refreshRequired = (reset && directory.loaded) || err.code === 'invalid_cursor' || err.code === 'cursor_not_found';
    directory.retry = true;
    directory.message = asError(err);
  } finally {
    if (directoryReads.get(directory) === request) { directoryReads.delete(directory); drawDirectory(directory); }
  }
}
async function loadTree(autoOpen = false) {
  ++state.treeVersion;
  if (!rootDirectory) {
    rootDirectory = makeDirectory(''); $('tree').replaceChildren(rootDirectory.box);
  }
  $('treeNotice').textContent = '';
  const version = state.fileVersion;
  const data = await readDirectory(rootDirectory,true);
  if (!data || !autoOpen || state.view !== 'files' || version !== state.fileVersion) return;
  const path = state.selection || data.entries.find(e => e.type === 'file' && e.name.toLowerCase() === 'readme.md')?.path ||
    data.entries.find(e => e.type === 'file')?.path;
  if (path) await openFile(path);
  else $('fileNotice').textContent = '从目录中选择一个文件。';
}

function selectedTurn() { return state.selectedRecord; }
function taskControls() {
  const provider = $('provider').value;
  const caps = state.providers;
  const blocked = !state.ready || state.sending || Boolean(state.pending);
  $('provider').disabled = $('prompt').disabled = !state.loadedKey;
  const proposalTarget = drawProposalTarget();
  updateProposalButtons();
  $('runAgent').disabled = blocked || !caps[provider]?.available || !$('prompt').value.trim() ||
    (provider === 'proposal' && !proposalTarget);
  const checkIssue = state.gitIssue || (caps.check?.available === false ? caps.check.detail : '');
  $('runCheck').disabled = blocked || !caps.check?.available || Boolean(state.gitIssue);
  $('checkHint').textContent = state.ready ? checkIssue || '' : '';
  $('checkHint').hidden = !$('checkHint').textContent;
  $('providerHint').textContent = !state.ready ? '等待连接' :
    caps[provider]?.available ? 'Ctrl / ⌘ + Enter' : '云机未提供此助手';
  $('pendingWrap').hidden = !state.pending || state.sending;
  $('retryStart').disabled = state.sending || !state.ready;
  const task = selectedTurn();
  const cancel = cancelRequests.get(task?.id);
  $('cancelTask').hidden = task?.canCancel !== true;
  $('cancelTask').disabled = !state.ready || Boolean(cancel?.pending);
  $('cancelTask').textContent = cancel?.pending ? '正在提交…' : '取消任务';
  $('cancelNotice').textContent = isActive(task) && task.status !== 'cancelling' ? cancel?.message || '' : '';
  const reply = $('replyPrompt').value;
  $('followUp').hidden = !task || (!task.canContinue && !reply);
  $('replyPrompt').readOnly = !task?.canContinue || state.pending?.parentTaskId === task?.id;
  $('sendReply').disabled = blocked || !task?.canContinue || !caps[task.provider]?.available || !reply.trim();
  $('replyHint').textContent = task?.canContinue ? (caps[task.provider]?.available ? 'Ctrl / ⌘ + Enter' : '云机未提供此助手') :
    (task?.nextTaskId ? '已有下一轮，可复制此草稿。' : '此回合无法续聊，可复制此草稿。');
  const target = state.feedbackParent && state.feedbackParent === state.selectedTask ? $('replyFeedback') : $('newTaskFeedback');
  if ($('submissionFeedback').parentElement !== target) target.append($('submissionFeedback'));
  drawPageControls(); drawOutputControls();
}
const taskTime = new Intl.DateTimeFormat('zh-CN',{hour:'2-digit',minute:'2-digit'});
const taskDay = new Intl.DateTimeFormat('zh-CN',{month:'numeric',day:'numeric'});
const taskDate = new Intl.DateTimeFormat('zh-CN',{year:'numeric',month:'numeric',day:'numeric'});
function taskLabel(task, today) {
  const label = names[task.provider] || task.provider;
  const prompt = (task.prompt || '').replace(/\s+/g,' ').trim().slice(0,38);
  const created = new Date(task.createdAt);
  let time = '';
  if (Number.isFinite(created.getTime())) {
    time = taskTime.format(created);
    if (created.toDateString() !== today.toDateString()) {
      const date = created.getFullYear() === today.getFullYear() ? taskDay : taskDate;
      time = date.format(created) + ' ' + time;
    }
  }
  return [label, prompt, labels[task.status] || task.status, time].filter(Boolean).join(' · ');
}
function mergeTask(previous, incoming) {
  if (!previous) return incoming;
  // A list snapshot may predate a cancellation, terminal event, or committed follow-up.
  if ((!isActive(previous) && isActive(incoming)) || (previous.status === 'cancelling' && incoming.canCancel)) return previous;
  if (previous.nextTaskId && !incoming.nextTaskId) return {...incoming,nextTaskId:previous.nextTaskId,canContinue:false};
  return incoming;
}
function updateTask(task) {
  if (!task?.id) return;
  if (!isActive(task)) cancelRequests.delete(task.id);
  const index = state.tasks.findIndex(item => item.id === task.id);
  if (index >= 0) state.tasks[index] = {...mergeTask(state.tasks[index],task),prompt:state.tasks[index].prompt};
  if (task.id === state.selectedTask) state.selectedRecord = mergeTask(selectedTurn(),task);
}
function drawPageControls() {
  const depth = state.historyAnchors.length;
  $('historyNav').hidden = !depth && !state.historyNext;
  const label = '第 ' + (depth + 1) + ' 页';
  if ($('historyPage').textContent !== label) $('historyPage').textContent = label;
  $('latestTasks').hidden = depth < 2;
  const target = state.historyTarget;
  $('latestTasks').disabled = !state.ready || target?.length === 0;
  $('newerTasks').disabled = !state.ready || !depth || Boolean(target && target.at(-1) === state.historyAnchors.at(-2));
  $('olderTasks').disabled = !state.ready || !state.historyNext || target?.at(-1) === state.historyNext;
}
function drawHistory() {
  const menu = $('taskHistory');
  const today = new Date();
  const rows = state.tasks.map(item => ({id:item.id,label:taskLabel(item,today)}));
  if (state.selectedTask && !state.tasks.some(item => item.id === state.selectedTask)) {
    const task = selectedTurn();
    rows.unshift({id:state.selectedTask, label:task ? '当前任务 · ' + taskLabel(task,today) : '正在读取当前任务…'});
  }
  if (!rows.length) rows.push({id:'',label:'暂无任务'});
  // Keep the opened native selector intact when polling only changes a status label.
  if (menu.options.length === rows.length && rows.every((row,index) => menu.options[index].value === row.id)) {
    rows.forEach((row,index) => {
      if (menu.options[index].textContent !== row.label) menu.options[index].textContent = row.label;
    });
  } else {
    menu.replaceChildren(...rows.map(row => new Option(row.label,row.id)));
  }
  if (menu.value !== (state.selectedTask || '')) menu.value = state.selectedTask || '';
  menu.disabled = !state.tasks.length && !state.selectedTask;
  drawPageControls();
}
function drawTaskTitle(task, reloadProposal = false) {
  showProposalReview(task, reloadProposal);
  $('eventHeading').hidden = !task;
  $('taskRequest').hidden = !task?.prompt;
  const prompt = task?.prompt || '';
  if ($('taskRequestText').textContent !== prompt) $('taskRequestText').textContent = prompt;
  $('turnNav').hidden = !task?.parentTaskId && !task?.nextTaskId;
  $('previousTurn').hidden = !task?.parentTaskId;
  $('nextTurn').hidden = !task?.nextTaskId;
  taskControls();
  if (!task) return;
  $('eventProvider').textContent = names[task.provider] || task.provider;
  $('taskStatus').textContent = labels[task.status] || task.status;
  $('taskStatus').dataset.status = task.status;
}
// Event cursors can refer to chunks of the same old database row. Treat them as
// opaque positions and always take navigation boundaries from the unmerged events.
let output = null;
const outputEncoder = new TextEncoder();
function resetOutput(taskId) {
  output = taskId ? {
    taskId, capability:null, events:[], earlier:false, history:null, liveCursor:0,
    poll:0, nav:0, pending:null, retry:null, notice:'', legacySize:0, clipped:false, needTail:true,
    reading:false, scrollTop:0, firstUnread:null, followOnReveal:false,
  } : null;
  $('eventLog').replaceChildren();
  drawOutputControls();
}
function outputBookmark(view, query) {
  remember(key('output.' + view.taskId),query ? JSON.stringify(query) : null,'sessionStorage');
}
function savedOutput(view) {
  try {
    const query = JSON.parse(stored(key('output.' + view.taskId),'null','sessionStorage'));
    for (const field of ['before','after']) {
      if (Number.isSafeInteger(query?.[field]) && query[field] > 0) return {[field]:query[field]};
    }
  } catch { /* The latest output is still accessible without a bookmark. */ }
  return null;
}
function drawOutputControls() {
  const view = output;
  const native = view?.capability === 'native';
  $('outputNav').hidden = !(native && (view.earlier || view.history || view.pending)) && !view?.clipped;
  $('outputPosition').textContent = view?.clipped ? '兼容模式' : view?.history ? '历史输出' : '最新输出';
  $('olderOutput').hidden = $('newerOutput').hidden = !native;
  $('olderOutput').disabled = !state.ready || !view?.earlier || Boolean(view.pending?.before);
  $('newerOutput').disabled = !state.ready || !view?.history || Boolean(view.pending?.after);
  $('latestOutput').hidden = !native || (!view.history && !view.pending && !view.retry);
  $('latestOutput').disabled = !state.ready || Boolean(view?.pending?.tail);
  const notice = view?.notice || (view?.clipped ? '此工作进程仅支持顺序读取，页面只显示最近一部分输出。' : '');
  $('outputFeedback').hidden = !notice;
  if ($('outputNotice').textContent !== notice) $('outputNotice').textContent = notice;
  $('retryOutput').hidden = !view?.retry;
  $('retryOutput').disabled = !state.ready || Boolean(view?.pending);
}
function appendOutput(container,event) {
  const text = String(event.text ?? '').slice(0,30000);
  if (!text) return 0;
  const last = container.lastElementChild;
  if (event.kind === 'delta' && last?.dataset.kind === 'delta' && last.firstChild.length + text.length <= 30000) {
    last.firstChild.appendData(text);
  } else {
    const row = el('div','event',text);
    row.dataset.kind = event.kind; container.append(row);
  }
  return text.length;
}
function nearOutputEnd(log) { return log.scrollHeight - log.scrollTop - log.clientHeight < 35; }
function followOutputEnd(view, log) {
  // A hidden mobile pane has no scrollable box until it becomes visible.
  view.followOnReveal = !log.clientHeight;
  if (!view.followOnReveal) {
    log.scrollTop = log.scrollHeight;
    view.scrollTop = log.scrollTop;
  }
}
function preserveReading(view, firstUnread) {
  if (!firstUnread || !view.events.length) return false;
  const log = $('eventLog'), selection = window.getSelection();
  const selected = selection && !selection.isCollapsed && log.contains(selection.anchorNode) && log.contains(selection.focusNode);
  if (!view.pending && !selected && !(view.reading && (!log.clientHeight || !nearOutputEnd(log)))) return false;
  // This is the first new event after exactly the content already on screen.
  view.history = {before:firstUnread};
  outputBookmark(view,view.history);
  return true;
}
function renderWindow(view, events, atEnd) {
  view.events = events;
  view.reading = false;
  const fragment = document.createDocumentFragment();
  for (const event of events) appendOutput(fragment,event);
  const log = $('eventLog');
  log.replaceChildren(fragment);
  if (atEnd) followOutputEnd(view,log);
  else {
    view.followOnReveal = false;
    log.scrollTop = 0; view.scrollTop = 0;
  }
}
// Forward replay may contain a full 256 KiB response. Use its prefix when moving
// one window forward, or its suffix when following live output.
function boundedOutput(events, newest) {
  let count = 0, bytes = 0;
  while (count < events.length && count < 160) {
    const index = newest ? events.length - count - 1 : count;
    const size = outputEncoder.encode(JSON.stringify(events[index])).length + 1;
    if (count && bytes + size > 64 * 1024) break;
    bytes += size; count++;
  }
  return newest ? events.slice(events.length - count) : events.slice(0,count);
}
function appendLiveOutput(view, added) {
  if (!added.length) return;
  const all = [...view.events,...added], events = boundedOutput(all,true);
  const log = $('eventLog');
  if (events.length < all.length) {
    view.earlier = true;
    renderWindow(view,events,true);
  } else {
    view.events = events;
    for (const event of added) appendOutput(log,event);
    followOutputEnd(view,log);
  }
}
function appendLegacyOutput(view, events) {
  const log = $('eventLog'), stick = nearOutputEnd(log);
  for (const event of events) {
    view.legacySize += appendOutput(log,event);
    while ((view.legacySize > 180000 || log.childElementCount > 400) && log.childElementCount > 1) {
      const first = log.firstElementChild;
      view.legacySize -= first.textContent.length;
      first.remove(); view.clipped = true;
    }
  }
  if (stick) followOutputEnd(view,log);
}
function scheduleEvents(ticket,delay) {
  clearTimeout(state.timer);
  state.timer = setTimeout(() => pollEvents(ticket),delay);
}
function updateOutputTask(task) {
  updateTask(task);
  drawTaskTitle(selectedTurn()); drawHistory();
}
async function pollEvents(ticket) {
  const view = output;
  if (!view || ticket !== state.taskVersion) return;
  const serial = ++view.poll;
  const current = () => output === view && ticket === state.taskVersion && serial === view.poll;
  const tail = view.needTail;
  try {
    const result = await rpc('events',{taskId:view.taskId,...(tail ? {tail:true} : {after:view.liveCursor})},current);
    if (!current()) return;
    const first = view.capability === null;
    if (tail) {
      view.capability = typeof result.hasEarlier === 'boolean' ? 'native' : 'legacy';
      view.needTail = false;
    }
    view.liveCursor = result.cursor;
    if (view.capability === 'legacy') {
      if (tail) {
        view.history = null; view.events = []; view.earlier = false; view.legacySize = 0;
        $('eventLog').replaceChildren(); outputBookmark(view,null);
      }
      appendLegacyOutput(view,result.events);
    } else if (!view.history) {
      if (tail) {
        if (!preserveReading(view,view.firstUnread)) {
          view.earlier = result.hasEarlier;
          renderWindow(view,result.events,true);
        }
        view.firstUnread = null;
      } else if (!preserveReading(view,result.events[0]?.seq)) {
        // A large live backlog is cheaper to replace with a tail request. Keep its
        // first unseen boundary in case the reader scrolls away before that arrives.
        if (result.hasMore) view.firstUnread = result.events[0]?.seq;
        else appendLiveOutput(view,result.events);
      }
    }
    view.needTail = view.capability === 'native' && result.hasMore === true;
    state.retryCount = 0;
    updateOutputTask(result.task);
    $('taskNotice').textContent = !view.liveCursor && !isActive(selectedTurn()) ? '此任务没有输出。' : '';
    const bookmark = first && view.capability === 'native' && savedOutput(view);
    if (bookmark) navigateOutput(bookmark);
    if (result.hasMore) scheduleEvents(ticket,0);
    else if (isActive(result.task)) scheduleEvents(ticket,1000);
  } catch(err) {
    if (!current()) return;
    if (['not_found','task_not_found','invalid_request','workspace_changed'].includes(err.code)) {
      if (err.code !== 'workspace_changed' && !selectedTurn()) selectTask('');
      $('taskNotice').textContent = asError(err);
    } else {
      state.retryCount++;
      $('taskNotice').textContent = asError(err) + ' 正在重连…';
      scheduleEvents(ticket,Math.min(12000,800 * (2 ** Math.min(4,state.retryCount))));
    }
  }
}
async function navigateOutput(query) {
  const view = output;
  if (!state.ready || view?.capability !== 'native') return;
  const serial = ++view.nav;
  const current = () => output === view && serial === view.nav;
  view.pending = query; view.retry = null; view.notice = '正在读取输出…'; drawOutputControls();
  try {
    const result = await rpc('events',{taskId:view.taskId,...query},current);
    if (!current()) return;
    if (!query.after && typeof result.hasEarlier !== 'boolean') {
      // The launcher now points to an older worker. Its tail/before response starts at zero.
      resetOutput(view.taskId); outputBookmark(output,null);
      scheduleEvents(state.taskVersion,0); return;
    }
    if (!query.tail && !result.events.length) {
      return navigateOutput({tail:true});
    }
    const events = query.after ? boundedOutput(result.events,false) : result.events;
    const history = query.before || (query.after && (result.hasMore || events.length < result.events.length));
    view.history = history ? query : null;
    view.earlier = query.after ? true : result.hasEarlier;
    view.pending = null; view.notice = '';
    outputBookmark(view,view.history);
    if (!view.history) {
      // Discard an in-flight poll from before the new live cursor.
      ++view.poll; view.liveCursor = result.cursor; view.needTail = false; view.firstUnread = null;
      clearTimeout(state.timer);
    }
    renderWindow(view,events,!view.history);
    updateOutputTask(result.task);
    $('taskNotice').textContent = !events.length && !isActive(selectedTurn()) ? '此任务没有输出。' : '';
    // A delayed live snapshot can precede a terminal status observed by a separate
    // poll. Read from this snapshot's cursor until its own terminal events arrive.
    if (!view.history && isActive(result.task)) scheduleEvents(state.taskVersion,isActive(selectedTurn()) ? 1000 : 0);
  } catch(err) {
    if (!current()) return;
    if (!query.tail && ['invalid_cursor','cursor_not_found'].includes(err.code)) {
      return navigateOutput({tail:true});
    }
    view.pending = null; view.retry = query; view.notice = asError(err); drawOutputControls();
  }
}
function browseOutput(query) {
  ++state.taskNavigation;
  navigateOutput(query);
}
async function cancelSelectedTask() {
  const task = selectedTurn();
  if (!state.ready || !task?.canCancel || cancelRequests.get(task.id)?.pending) return;
  const version = state.workspaceVersion;
  cancelRequests.set(task.id,{pending:true}); taskControls();
  try {
    const result = await rpc('cancel',{taskId:task.id});
    if (version !== state.workspaceVersion) return;
    cancelRequests.delete(task.id);
    updateTask(result);
    drawHistory();
  } catch(err) {
    if (version !== state.workspaceVersion) return;
    const uncertain = ['transport','network','timeout','worker_error','invalid_response'].includes(err.code);
    if (cancelRequests.has(task.id)) cancelRequests.set(task.id,{message:asError(err) +
      (uncertain ? ' 取消结果尚未确认，可再次取消此任务。' : '')});
  }
  if (state.selectedTask === task.id) {
    drawTaskTitle(selectedTurn());
    // Keep the log and its confirmed cursor, and discard any poll started before this receipt.
    if (state.ready) scheduleEvents(++state.taskVersion,0);
  }
}
function selectTask(id, userNavigation = false, record = null) {
  if (userNavigation) ++state.taskNavigation;
  const next = ++state.taskVersion;
  clearTimeout(state.timer);
  state.selectedRecord = record || state.tasks.find(t => t.id === id) || null;
  state.selectedTask = id || null; state.retryCount = 0;
  remember(key('task'),state.selectedTask);
  remember(key('task'),state.selectedTask,'sessionStorage');
  resetOutput(state.selectedTask); $('taskRequest').open = false;
  $('replyPrompt').value = id ? stored(key('reply.' + id),'','sessionStorage') : '';
  $('followUp').open = Boolean($('replyPrompt').value);
  drawTaskTitle(selectedTurn()); drawHistory();
  $('taskNotice').textContent = id ? '正在读取任务输出…' : '运行一次项目检查或编码任务后，可在这里查看输出。';
  if (id) pollEvents(next);
}
function restoredHistoryAnchors() {
  try {
    const anchors = JSON.parse(stored(key('historyAnchors'),'[]','sessionStorage'));
    if (Array.isArray(anchors) && anchors.every(id => typeof id === 'string' && id.length && id.length <= 128)) return anchors;
  } catch { /* An incomplete navigation history can start on the newest page. */ }
  return [];
}
function browseHistory(anchors) {
  ++state.taskNavigation;
  loadTasks(anchors,false);
}
async function loadTasks(anchors = state.historyAnchors, refreshSelected = true, notice = '') {
  if (!state.loadedKey) return;
  const ticket = ++state.taskListVersion;
  const before = anchors.at(-1);
  state.historyTarget = anchors;
  $('historyNotice').textContent = '正在读取任务记录…'; drawPageControls();
  if (!state.selectedTask) {
    const saved = stored(key('task'),stored(key('task')),'sessionStorage');
    if (saved) selectTask(saved);
  }
  try {
    const data = await rpc('tasks',before ? {beforeTaskId:before} : {},() => ticket === state.taskListVersion);
    if (ticket !== state.taskListVersion) return;
    const known = new Map(state.tasks.map(task => [task.id,task]));
    if (selectedTurn()) known.set(state.selectedTask,selectedTurn());
    state.tasks = data.tasks.slice(0,80).map(task => mergeTask(known.get(task.id),task));
    // A launcher serving the original API returns its newest page for every request.
    state.historyAnchors = Object.hasOwn(data,'nextBeforeTaskId') ? anchors : [];
    const next = data.nextBeforeTaskId;
    state.historyNext = typeof next === 'string' && next && !anchors.includes(next) ? next : null;
    remember(key('historyAnchors'),state.historyAnchors.length ? JSON.stringify(state.historyAnchors) : null,'sessionStorage');
    $('historyNotice').textContent = notice;
    if (!state.selectedTask) { selectTask(state.tasks[0]?.id || ''); return; }
    const previous = selectedTurn(), summary = state.tasks.find(task => task.id === state.selectedTask);
    if (summary) state.selectedRecord = previous ? {...summary,prompt:previous.prompt} : summary;
    drawHistory(); drawTaskTitle(selectedTurn(),refreshSelected);
    if (refreshSelected && previous && !summary && !isActive(previous)) pollEvents(state.taskVersion);
  } catch(err) {
    if (ticket !== state.taskListVersion) return;
    if (before && ['cursor_not_found','invalid_cursor'].includes(err.code)) {
      return loadTasks([],refreshSelected,'原记录页已失效，已返回最近记录。');
    }
    $('historyNotice').textContent = asError(err);
  } finally {
    if (ticket === state.taskListVersion) { state.historyTarget = null; drawPageControls(); }
  }
}
function rememberPending(value) {
  const request = value || state.pending;
  state.pending = value;
  const serialized = value ? JSON.stringify(value) : null;
  // Each acknowledged request removes only its own record, even across tabs.
  remember(key('pending.' + request.requestId),serialized);
  remember(key('pending'),serialized,'sessionStorage');
}
function restorePending() {
  const parse = raw => {
    try {
      const request = JSON.parse(raw);
      if (request && typeof request.requestId === 'string' && typeof request.prompt === 'string' &&
          Object.hasOwn(names,request.provider) && (!request.parentTaskId || typeof request.parentTaskId === 'string')) return request;
    } catch { /* Ignore incomplete records. */ }
    return null;
  };
  const legacy = parse(stored(key('pending')));
  if (legacy) {
    remember(key('pending.' + legacy.requestId),JSON.stringify(legacy));
    if (stored(key('pending.' + legacy.requestId))) remember(key('pending'),null);
  }
  const current = parse(stored(key('pending'),'','sessionStorage'));
  if (current) return current;
  // A reopened tab can also recover submissions left by a closed browser.
  try {
    const prefix = 'cloudwork.' + key('pending.');
    for (let i = 0; i < localStorage.length; ++i) {
      const entry = localStorage.key(i);
      if (!entry?.startsWith(prefix)) continue;
      const request = parse(localStorage.getItem(entry));
      if (request) return request;
    }
  } catch { /* Local storage may be unavailable. */ }
  return legacy;
}
async function submitStart(request) {
  if (state.sending || !request) return;
  const version = state.workspaceVersion;
  const navigation = state.taskNavigation;
  state.feedbackParent = request.parentTaskId || null;
  state.sending = true; rememberPending(request); taskControls();
  $('startNotice').textContent = '正在提交到云端…';
  try {
    const item = await rpc('start',request);
    if (state.workspaceVersion !== version) return;
    updateTask(item); rememberPending(null);
    if (request.parentTaskId) {
      const parent = state.selectedTask === request.parentTaskId ? selectedTurn() :
        state.tasks.find(task => task.id === request.parentTaskId);
      if (parent) updateTask({...parent,canContinue:false,nextTaskId:item.id});
      const draftKey = key('reply.' + request.parentTaskId);
      if (stored(draftKey,'','sessionStorage').trim() === request.prompt) remember(draftKey,null,'sessionStorage');
      if (state.selectedTask === request.parentTaskId && $('replyPrompt').value.trim() === request.prompt) $('replyPrompt').value = '';
    } else if (request.provider === 'proposal') {
      acknowledgeProposalDraft(request);
    } else if (request.provider !== 'check' && $('provider').value !== 'proposal' && $('prompt').value.trim() === request.prompt) {
      $('prompt').value = ''; remember(key('prompt'),null);
    }
    $('startNotice').textContent = '';
    if (state.taskNavigation === navigation) selectTask(item.id,false,item);
    else { drawHistory(); drawTaskTitle(selectedTurn()); }
    // Reload membership from the server so insertion at the head cannot skip a page boundary.
    if (!state.historyAnchors.length && !state.historyTarget) loadTasks();
  } catch(err) {
    if (state.workspaceVersion !== version) return;
    if (['invalid_request','invalid_argument','bad_request','unknown_provider','unavailable','provider_unavailable','invalid_prompt',
         'conflict','not_found','invalid_path','unsupported_file','too_large'].includes(err.code)) {
      rememberPending(null); $('startNotice').textContent = asError(err);
      if (request.provider === 'proposal' && err.code === 'conflict' && state.selection === request.path && state.view === 'files') openFile(request.path);
      if (request.parentTaskId) loadTasks();
    } else {
      $('pendingMessage').textContent = pendingLabel(request) + asError(err) + ' 重试将沿用此次请求编号。';
      $('startNotice').textContent = '';
    }
  } finally {
    if (state.workspaceVersion === version) { state.sending = false; taskControls(); }
  }
}
function pendingLabel(request) {
  if (request.provider === 'proposal') return '为 ' + request.path + ' 生成修改建议的请求尚未确认。';
  return request.parentTaskId ? '追问「' + request.prompt.slice(0,40) + '」尚未确认。' : '';
}
function sendReply() {
  const task = selectedTurn(), prompt = $('replyPrompt').value.trim();
  if (state.pending || state.sending || !state.ready || !task?.canContinue || !state.providers[task.provider]?.available || !prompt) return;
  submitStart({requestId:crypto.randomUUID(),provider:task.provider,prompt,parentTaskId:task.id});
}
function run(provider) {
  if (state.pending || state.sending || !state.ready || !state.providers[provider]?.available) return;
  if (provider === 'check' && state.gitIssue) return;
  const prompt = provider === 'check' ? '' : $('prompt').value.trim();
  if (provider !== 'check' && !prompt) return;
  const target = provider === 'proposal' ? drawProposalTarget() : {};
  if (!target) return;
  submitStart({requestId:crypto.randomUUID(),provider,prompt,...target});
}
function restore(status) {
  state.workspaceId = status.workspaceId;
  const projectName = String(status.project || '项目');
  const scope = workspaceScope(status);
  restoreFileScope(scope);
  state.mode = status.mode;
  const firstCapabilities = !state.providers.codex && Boolean(status.providers?.codex);
  state.project = projectName; state.providers = status.providers || {};
  state.diffScopes = Array.isArray(status.diffScopes) && ['staged','unstaged'].every(scope => status.diffScopes.includes(scope));
  $('topProject').textContent = projectName;
  updateGitStatus(status);
  if (state.loadedKey === scope) {
    if (!stored(scope + '.provider') && $('provider').value === 'codex' &&
        !state.providers.codex?.available && state.providers.claude?.available) $('provider').value = 'claude';
    return;
  }
  const provider = stored(scope + '.provider',stored(scope + '.provider'),'sessionStorage');
  if (state.loadedKey !== scope || (firstCapabilities && !provider)) {
    $('provider').value = ['codex','claude','proposal'].includes(provider) ? provider :
      (!state.providers.codex?.available && state.providers.claude?.available ? 'claude' : 'codex');
  }
  ++state.workspaceVersion;
  cancelRequests.clear();
  if (state.loadedKey) {
    ++state.taskVersion; ++state.fileVersion; ++state.taskListVersion;
    clearTimeout(state.timer); state.tasks = []; state.selectedTask = null; state.selectedRecord = null;
    state.pending = null; state.sending = false; resetOutput(null);
    $('fileNotice').textContent = ''; $('startNotice').textContent = '';
    $('replyPrompt').value = ''; $('followUp').open = false; $('taskRequest').open = false;
    $('pendingMessage').textContent = '';
  }
  state.loadedKey = scope;
  state.historyAnchors = restoredHistoryAnchors(); state.historyNext = null; state.historyTarget = null;
  $('historyNotice').textContent = '';
  restoreComposerDraft();
  state.pending = restorePending();
  state.feedbackParent = state.pending?.parentTaskId || null;
  if (state.pending) $('pendingMessage').textContent = (pendingLabel(state.pending) || '上次提交尚未确认。') + '可以用原请求编号再次提交。';
  drawHistory(); drawTaskTitle(null);
}
async function recoverFromLauncher(version) {
  try {
    const response = await fetch('/api/workspace',{cache:'no-store',signal:AbortSignal.timeout(5000)});
    if (!response.ok) return;
    const identity = await response.json();
    if (version !== state.refreshVersion || state.ready || !identity.ok || !identity.data?.workspaceId) return;
    restore(identity.data);
    taskControls();
    const path = drafts.has(state.selection) ? state.selection : drafts.keys().next().value;
    if (path && (!state.file || state.file.path === path)) await openFile(path);
    else if (!state.file) $('fileNotice').textContent = '连接云端后可读取项目文件。';
  } catch { /* Only restore after the launcher has confirmed the workspace identity. */ }
}
async function refresh() {
  const version = ++state.refreshVersion;
  connection('loading');
  // A slow cloud status request must not block editing a draft in the local launcher.
  const recovery = !state.fileScope ? recoverFromLauncher(version) : null;
  try {
    const status = await rpc('status');
    if (version !== state.refreshVersion) return;
    restore(status); state.ready = true; connection('online'); taskControls(); drawEdit();
    const openDraft = state.view === 'files' && drafts.has(state.selection);
    await Promise.all([loadTree(state.view === 'files' && !openDraft),loadTasks(),
      openDraft ? openFile(state.selection) : state.view === 'diff' ? openDiff() : Promise.resolve()]);
  } catch(err) {
    if (version !== state.refreshVersion) return;
    connection('error'); state.ready = false; taskControls();
    $('treeNotice').textContent = asError(err); $('taskNotice').textContent = '刷新工作区可重新连接。';
    if (!state.file) $('fileNotice').textContent = asError(err);
    drawEdit();
    await (recovery || recoverFromLauncher(version));
  }
}

$('mobileFiles').addEventListener('click', () => mobilePage('files'));
$('mobileTasks').addEventListener('click', () => mobilePage('tasks'));
$('refresh').addEventListener('click',refresh);
$('reloadTree').addEventListener('click',() => loadTree());
$('reloadTasks').addEventListener('click',() => loadTasks());
$('olderTasks').addEventListener('click',() => browseHistory([...state.historyAnchors,state.historyNext]));
$('newerTasks').addEventListener('click',() => browseHistory(state.historyAnchors.slice(0,-1)));
$('latestTasks').addEventListener('click',() => browseHistory([]));
$('showDiff').addEventListener('click',openDiff);
$('showFiles').addEventListener('click',() => { viewButtons('files'); if (state.selection) openFile(state.selection); else loadTree(true); });
$('editFile').addEventListener('click',beginEdit);
$('cancelEdit').addEventListener('click',cancelEdit);
$('saveFile').addEventListener('click',saveEdit);
$('fileDrafts').addEventListener('change',() => { if ($('fileDrafts').value) openFile($('fileDrafts').value); });
$('fileEditArea').addEventListener('input',() => {
  const edit = drafts.get(state.file?.path);
  if (!edit) return;
  const area = $('fileEditArea');
  edit.content = editedFileText(edit,area.value,area.selectionEnd);
  if (!edit.conflict && !edit.saving) edit.message = '';
  persistDraft(state.file.path,edit);
  drawEdit();
});
$('fileEditArea').addEventListener('keydown',event => {
  if (!event.isComposing && (event.ctrlKey || event.metaKey) && event.key.toLowerCase() === 's') { event.preventDefault(); saveEdit(); }
});
$('taskHistory').addEventListener('change',() => selectTask($('taskHistory').value,true));
document.getElementById('olderOutput').addEventListener('click',() => browseOutput({before:output.events[0].seq}));
document.getElementById('newerOutput').addEventListener('click',() => browseOutput({after:output.events.at(-1).seq}));
document.getElementById('latestOutput').addEventListener('click',() => browseOutput({tail:true}));
document.getElementById('retryOutput').addEventListener('click',() => browseOutput(output.retry));
new ResizeObserver(() => {
  const log = $('eventLog');
  if (output?.followOnReveal && !output.history && log.clientHeight) followOutputEnd(output,log);
}).observe($('eventLog'));
document.getElementById('eventLog').addEventListener('scroll',event => {
  if (!output || output.history || !output.events.length) return;
  const log = event.currentTarget;
  if (!log.clientHeight) return;
  if (nearOutputEnd(log)) output.reading = false;
  else if (log.scrollTop < output.scrollTop) output.reading = true;
  output.scrollTop = log.scrollTop;
});
$('cancelTask').addEventListener('click',cancelSelectedTask);
$('previousTurn').addEventListener('click',() => selectTask(selectedTurn().parentTaskId,true));
$('nextTurn').addEventListener('click',() => selectTask(selectedTurn().nextTaskId,true));
$('replyPrompt').addEventListener('input',() => {
  if (state.selectedTask) remember(key('reply.' + state.selectedTask),$('replyPrompt').value || null,'sessionStorage');
  taskControls();
});
$('replyPrompt').addEventListener('keydown',event => {
  if (!event.isComposing && (event.ctrlKey || event.metaKey) && event.key === 'Enter') { event.preventDefault(); sendReply(); }
});
$('sendReply').addEventListener('click',sendReply);
$('provider').addEventListener('change',() => {
  remember(key('provider'),$('provider').value);
  remember(key('provider'),$('provider').value,'sessionStorage');
  restoreComposerDraft(); taskControls();
});
$('prompt').addEventListener('input',() => {
  if (state.loadedKey) {
    if ($('provider').value === 'proposal') saveProposalDraft();
    else remember(key('prompt'),$('prompt').value);
  }
  taskControls();
});
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
  if (unbackedDrafts.size || [...drafts.values()].some(draft => draft.content !== draft.base || draft.submitted !== undefined)) {
    event.preventDefault(); event.returnValue = '';
  }
});
refresh();

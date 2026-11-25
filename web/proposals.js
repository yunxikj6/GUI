'use strict';

// Keep an unsent instruction paired with the version its author was looking at.
let proposalDraft = null;
const proposalLimit = 24576;
function currentProposalTarget() {
  const file = state.file;
  if (state.view !== 'files' || file?.path !== state.selection || file?.kind !== 'text' ||
      !file.revision || file.size > proposalLimit || drafts.has(file.path) || $('fileSync').textContent) return null;
  return {path:file.path, revision:file.revision};
}
function restoreComposerDraft() {
  proposalDraft = null;
  if ($('provider').value !== 'proposal') {
    $('prompt').value = stored(key('prompt'));
    return;
  }
  try {
    const draft = JSON.parse(stored(key('proposalDraft'),'null','sessionStorage'));
    if (draft && typeof draft.text === 'string') {
      const target = draft.target;
      proposalDraft = {text:draft.text,
        target:typeof target?.path === 'string' && /^[a-f0-9]{64}$/.test(target.revision) ? target : null,
        waitingPath:typeof draft.waitingPath === 'string' ? draft.waitingPath : null};
    }
  } catch { /* An incomplete draft cannot identify a target. */ }
  $('prompt').value = proposalDraft?.text || '';
}
function persistProposalDraft() {
  const saved = proposalDraft && {text:proposalDraft.text,target:proposalDraft.target,waitingPath:proposalDraft.waitingPath};
  remember(key('proposalDraft'),saved && JSON.stringify(saved),'sessionStorage');
}
function saveProposalDraft(replaceTarget = false) {
  const text = $('prompt').value;
  if (!text) proposalDraft = null;
  else if (!proposalDraft || replaceTarget) {
    const file = state.file;
    const path = state.view === 'files' ? state.selection : null;
    // A cached preview pins the visible version even while revalidation is in flight.
    const target = file && file.path === path && file.kind === 'text' && file.revision && file.size <= proposalLimit ?
      {path,revision:file.revision} : null;
    proposalDraft = {text, target, waitingPath:target ? null : path, waitingVersion:state.fileVersion};
  } else proposalDraft.text = text;
  persistProposalDraft();
}
function sameProposalTarget(a,b) {
  return Boolean(a && b && a.path === b.path && a.revision === b.revision);
}
function acknowledgeProposalDraft(request) {
  let draft = proposalDraft;
  if (!draft) {
    try { draft = JSON.parse(stored(key('proposalDraft'),'null','sessionStorage')); } catch { return; }
  }
  if (typeof draft?.text !== 'string' || draft.text.trim() !== request.prompt || !sameProposalTarget(draft.target,request)) return;
  remember(key('proposalDraft'),null,'sessionStorage');
  proposalDraft = null;
  if ($('provider').value === 'proposal') $('prompt').value = '';
}
function drawProposalTarget() {
  const active = $('provider').value === 'proposal';
  $('proposalTarget').hidden = !active;
  if (!active) return null;
  const current = currentProposalTarget();
  // Before any preview existed the author could still have selected this path.
  // Only its original in-flight read may complete that choice automatically.
  if (current && proposalDraft?.waitingPath === current.path && proposalDraft.waitingVersion === state.fileVersion) {
    proposalDraft.target = current;
    proposalDraft.waitingPath = null;
    persistProposalDraft();
  }
  const target = proposalDraft ? proposalDraft.target : current;
  const matches = sameProposalTarget(target,current);
  const caption = target ? target.path + ' · ' + target.revision.slice(0,10) : (proposalDraft?.waitingPath || '');
  const file = state.file;
  let message = '';
  if (!current) {
    if (file && drafts.has(file.path)) message = '请先保存文件或退出编辑。';
    else if (file?.size > proposalLimit) message = '修改建议支持 24 KiB 以内的文本文件。';
    else if (proposalDraft?.waitingPath && proposalDraft.waitingPath === state.selection && !file) message = '读取此文件版本后可提交。';
    else message = '请选择一个已加载的文本文件。';
  } else if (!matches) message = '请确认是否使用当前文件版本。';
  $('proposalTargetText').textContent = [caption, message].filter(Boolean).join('　');
  $('proposalTargetText').title = target ? target.path + ' · ' + target.revision : '';
  $('useProposalFile').hidden = !proposalDraft || !current || matches;
  $('useProposalFile').disabled = state.sending || Boolean(state.pending);
  return matches ? target : null;
}

const decisionLabels = {
  pending:'等待审核', applied:'已应用修改', rejected:'已拒绝修改', generating:'正在生成修改建议', failed:'建议生成失败',
};
let proposalReview = null;
function showProposalReview(task, reload = false) {
  if (task?.provider !== 'proposal' || task.status !== 'succeeded') {
    proposalReview = null;
    $('proposalReview').hidden = true;
    $('proposalDiff').textContent = '';
    return;
  }
  if (!proposalReview || proposalReview.id !== task.id || proposalReview.scope !== state.loadedKey) {
    proposalReview = {id:task.id, version:state.taskVersion, scope:state.loadedKey, data:null, busy:false, retryDecision:null};
    $('proposalReview').hidden = false;
    loadProposalReview(proposalReview);
  } else if (reload || proposalReview.version !== state.taskVersion) {
    proposalReview.version = state.taskVersion;
    loadProposalReview(proposalReview);
  }
}
function drawProposalReview(review, message = '') {
  if (proposalReview !== review) return;
  const data = review.data;
  $('proposalPath').textContent = data?.path || '文件修改建议';
  $('proposalStatus').textContent = message || decisionLabels[data?.decision] || '';
  const diff = data?.diff || '';
  if ($('proposalDiff').textContent !== diff) $('proposalDiff').textContent = diff;
  $('proposalDiff').hidden = !diff;
  const pending = data?.decision === 'pending' && Boolean(diff);
  $('applyProposal').hidden = $('rejectProposal').hidden = !pending;
  updateProposalButtons();
  $('reloadProposal').hidden = review.busy || (!message && !pending);
}
function updateProposalButtons() {
  const review = proposalReview;
  if (!review) return;
  for (const [id,decision] of [['applyProposal','apply'],['rejectProposal','reject']]) {
    $(id).disabled = !state.ready || state.sending || review.busy || Boolean(review.retryDecision && review.retryDecision !== decision);
  }
}
function refreshAfterApproval(review, data) {
  if (review.scope !== state.loadedKey || data.decision !== 'applied' || review.seenApplied) return;
  review.seenApplied = true;
  forgetFile(data.path);
  if (state.view === 'files' && state.selection === data.path) openFile(data.path);
  else if (state.view === 'diff') openDiff();
}
async function loadProposalReview(review) {
  if (!review || review.busy) return;
  review.busy = true;
  drawProposalReview(review, '正在读取修改建议…');
  let message = '';
  try {
    const data = await rpc('proposal',{taskId:review.id});
    if (proposalReview !== review) return;
    review.data = data;
    review.retryDecision = null;
    refreshAfterApproval(review,data);
  } catch (err) {
    message = asError(err);
  } finally {
    review.busy = false;
    drawProposalReview(review,message);
  }
}
async function decideProposal(decision) {
  const review = proposalReview;
  if (!state.ready || state.sending || !review || review.busy || review.data?.decision !== 'pending' ||
      (review.retryDecision && review.retryDecision !== decision)) return;
  review.busy = true;
  drawProposalReview(review, decision === 'apply' ? '正在应用修改…' : '正在拒绝修改…');
  let message = '';
  let reload = false;
  try {
    const data = await rpc('decideProposal',{taskId:review.id,decision});
    review.data = data;
    review.retryDecision = null;
    refreshAfterApproval(review,data);
  } catch (err) {
    reload = err.code === 'proposal_decided';
    if (!['conflict','invalid_path','not_found','invalid_request','unsupported_file','too_large','proposal_decided','workspace_changed'].includes(err.code)) {
      review.retryDecision = decision;
      message = '操作结果尚未确认。可重新读取，或重试刚才的操作。';
    } else message = asError(err);
  } finally {
    review.busy = false;
    drawProposalReview(review,message);
    if (reload && proposalReview === review) loadProposalReview(review);
  }
}
document.getElementById('useProposalFile').addEventListener('click',() => { saveProposalDraft(true); taskControls(); });
document.getElementById('applyProposal').addEventListener('click',() => decideProposal('apply'));
document.getElementById('rejectProposal').addEventListener('click',() => decideProposal('reject'));
document.getElementById('reloadProposal').addEventListener('click',() => loadProposalReview(proposalReview));

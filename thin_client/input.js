// The hidden textarea is an input-method target, never a mirror of the cloud
// editor. Its current preedit belongs to the OS and is cleared after commit.
const navigation = ['ArrowLeft', 'ArrowRight', 'ArrowUp', 'ArrowDown', 'Home', 'End'];
const chords = [
  new Set([...navigation, 'Tab', 'Enter', 'Escape', 'Backspace', 'Delete', 'PageUp', 'PageDown']),
  new Set([...navigation, 'Tab', 'Enter', 'PageUp', 'PageDown']),
  new Set([...navigation, 'Backspace', 'Delete', 'a', 'z', 'y']),
  new Set([...navigation, 'z']),
];
const bytes = new TextEncoder();

export function textSize(text) { return bytes.encode(text).length; }

export function attachKeyboard(field, canvas, send, announce, anchor, generation) {
  let composing = false;
  let startedIn;
  let lastCommit;
  let releaseCommit;
  function forgetCommit() {
    clearTimeout(releaseCommit);
    lastCommit = undefined;
  }
  function text(value, sequence) {
    if (!value) return;
    if (value.length > 4096 || textSize(value) > 4096 || /[\u0000-\u0008\u000b-\u001f\u007f\ud800-\udfff]/u.test(value)) {
      announce('这段文字未发送。每次最多输入 4096 个 UTF-8 字节，支持换行和制表符。');
      return;
    }
    send({type: 'text', text: value, sequence});
  }
  function key(name, modifiers = 0) {
    send({type: 'key', key: name, modifiers});
  }
  field.addEventListener('keydown', event => {
    if (composing || event.isComposing || event.keyCode === 229) return;
    forgetCommit();
    if (event.altKey || event.metaKey || event.getModifierState('AltGraph')) return;
    const modifiers = Number(event.shiftKey) + 2 * Number(event.ctrlKey);
    const name = event.ctrlKey && event.key.length === 1 ? event.key.toLowerCase() : event.key;
    if (chords[modifiers].has(name)) {
      event.preventDefault();
      key(name, modifiers);
    }
  });
  field.addEventListener('compositionstart', () => {
    forgetCommit();
    composing = true;
    startedIn = anchor();
  });
  field.addEventListener('compositionend', event => {
    composing = false;
    field.value = '';
    lastCommit = event.data;
    // Some engines deliver the final insertText after compositionend in the
    // same edit. A fresh keystroke, paste or composition starts another edit.
    releaseCommit = setTimeout(forgetCommit, 0);
    if (event.data && startedIn?.generation !== generation()) {
      announce('输入期间云端窗口或本机焦点发生了变化，这次输入法确认未发送。');
    } else if (event.data) {
      text(event.data, startedIn.sequence);
    }
  });
  field.addEventListener('beforeinput', event => {
    if (composing || event.isComposing) return;
    // On-screen keyboards may produce editing intentions without keydown.
    const editing = {'deleteContentBackward': 'Backspace', 'deleteContentForward': 'Delete',
                     'insertLineBreak': 'Enter', 'insertParagraph': 'Enter'}[event.inputType];
    if (editing && event.cancelable) {
      event.preventDefault();
      key(editing);
    }
  });
  field.addEventListener('input', event => {
    if (event.isComposing || composing) return;
    const value = field.value;
    field.value = '';
    if (event.inputType === 'insertFromComposition' ||
        (lastCommit !== undefined && (event.data === lastCommit || value === lastCommit))) {
      forgetCommit();
      return;
    }
    text(value);
  });
  field.addEventListener('paste', event => {
    event.preventDefault();
    if (composing) {
      announce('请先确认或取消当前输入法组合，再粘贴。');
      return;
    }
    forgetCommit();
    const value = event.clipboardData?.getData('text/plain');
    if (value) text(value.replace(/\r\n?/g, '\n'));
  });
  field.addEventListener('blur', () => { startedIn = undefined; });
  // Keep local composition candidates near the part of the original surface
  // the user selected. The text field never intercepts pointer hit testing.
  canvas.addEventListener('pointerdown', event => {
    if (event.button !== 0) return;
    // The proxy stays focused even if this click selects another cloud editor.
    // A composition begun before that click must never commit into the new one.
    startedIn = undefined;
    event.preventDefault();
    field.style.left = Math.max(0, Math.min(innerWidth - 2, event.clientX)) + 'px';
    field.style.top = Math.max(0, Math.min(innerHeight - 18, event.clientY)) + 'px';
    field.focus({preventScroll: true});
  });
  field.focus({preventScroll: true});
}

"""Serve a single cloud-owned application over a paired, forwarded loopback port."""
from __future__ import annotations

import argparse
import asyncio
from contextlib import suppress
import hmac
import os
from pathlib import Path
import re
import secrets
import sys

from .wire import (CONTROL, SHIFT, INPUT_TYPES, Click, InputAck,
                   TextCommit, WireError, async_read_message, encode_message, validate_click)


AUTH_PREFIX = b'CLRP-AUTH/1 '
TOKEN_PATTERN = re.compile(r'[A-Za-z0-9_-]{43}\Z')


def same_scene(left, right):
    return left.width == right.width and left.height == right.height and left.layers == right.layers


# Keep this reference solely inside the private source process. No DOM attributes,
# input values, selection contents or focus identifiers are sent to the viewer.
_FOCUSED = """() => {
    let node = document.activeElement;
    for (;;) {
        const next = node?.shadowRoot?.activeElement || node?.contentDocument?.activeElement;
        if (!next) return node;
        node = next;
    }
}"""
# This state stays in the original renderer. Track native focus, selection and
# programmatic value changes even when the visible pixels only show an animation.
_REMEMBER_FOCUS = """() => {
    const node = (""" + _FOCUSED + """)();
    const doc = node?.ownerDocument;
    const selection = doc?.getSelection();
    const form = !!node?.matches('textarea, input');
    const saved = {
        node, form, dirty: false, value: form ? node.value : undefined,
        start: form ? node.selectionStart : selection?.anchorNode,
        end: form ? node.selectionEnd : selection?.focusNode,
        anchorOffset: selection?.anchorOffset, focusOffset: selection?.focusOffset,
        direction: form ? node.selectionDirection : undefined,
    };
    const invalidate = () => { saved.dirty = true; };
    node?.addEventListener('blur', invalidate, true);
    node?.addEventListener('input', invalidate, true);
    const observer = node?.isContentEditable ? new MutationObserver(invalidate) : null;
    observer?.observe(node, {childList:true, characterData:true, subtree:true});
    saved.cleanup = () => {
        node?.removeEventListener('blur', invalidate, true);
        node?.removeEventListener('input', invalidate, true);
        observer?.disconnect();
    };
    return saved;
}"""
_FOCUS_MATCH = """(s, purpose) => {
    const n = (""" + _FOCUSED + """)();
    if (n !== s.node || s.dirty) return false;
    const selection = n?.ownerDocument.getSelection();
    if (s.form && (s.value !== n.value || s.start !== n.selectionStart ||
        s.end !== n.selectionEnd || s.direction !== n.selectionDirection)) return false;
    if (n?.isContentEditable && (s.start !== selection?.anchorNode ||
        s.end !== selection?.focusNode || s.anchorOffset !== selection?.anchorOffset ||
        s.focusOffset !== selection?.focusOffset)) return false;
    if (purpose === 'observe') return true;
    if (!n || n.localName === 'iframe' || n.localName === 'frame') return false;
    return purpose === 'key' || n.isContentEditable || n.matches('textarea:read-write, input:read-write');
}"""

# CDP Input.insertText targets whichever element is focused when its message
# eventually executes. A vendor beforeinput listener can even change that target
# while handling the edit. Bind insertion to our saved native editor in the same
# renderer task, preserving DOM cancellation, native editing, input and undo.
_COMMIT = """(saved, text) => {
    const matches = (""" + _FOCUS_MATCH + """);
    if (!matches(saved, 'edit')) return false;
    const node = saved.node;
    const doc = node.ownerDocument;
    const intent = new doc.defaultView.InputEvent('beforeinput', {
        inputType:'insertText', data:text, bubbles:true, composed:true, cancelable:true
    });
    if (!node.dispatchEvent(intent)) return true; // application handled the intent
    if (!matches(saved, 'edit')) return false;
    return doc.execCommand('insertText', false, text);
}"""

# Keep the ownership of sampled logical pixels inside the cloud renderer.
# Tiny controls can be sampled in full. Large buttons get a central patch. A
# point outside all certified regions requires the ordinary full-scene check.
_CLICK_OWNER = """el => el?.closest('button, [role="button"], input, textarea')"""
_BUTTON_STATE = """el => JSON.stringify([
    el.textContent?.slice(0,1024), el.getAttribute('aria-label'), el.getAttribute('aria-expanded'),
    el.getAttribute('aria-pressed'), el.getAttribute('title'), el.name, el.type,
    el.formAction, el.formMethod, el.form?.action, el.form?.method])"""
_FIELD_HITS = """(s, seq) => {
    if (!s.hits) s.hits = new Map();
    const rows = [];
    const owner = (""" + _CLICK_OWNER + """), signature = (""" + _BUTTON_STATE + """);
    const field = s.node?.matches('textarea:read-write, input:read-write') ? s.node : null;
    let budget = 32768;
    const buttons = document.querySelectorAll('button, [role="button"]');
    for (const n of [field, ...Array.from(buttons).slice(0,32)]) {
        if (!n || n.ownerDocument !== document || n.matches(':disabled,[aria-disabled=true]')) continue;
        const r = n.getBoundingClientRect();
        let x = Math.max(0,Math.ceil(r.left)), y = Math.max(0,Math.ceil(r.top));
        let w = Math.min(innerWidth,Math.floor(r.right))-x, h = Math.min(innerHeight,Math.floor(r.bottom))-y;
        if (w<=0 || h<=0) continue;
        // Limit sampling cost to one small central band per large element.
        const mw = Math.min(w, n===field ? 256 : 64), mh = Math.min(h, n===field ? 24 : 20);
        x += Math.floor((w-mw)/2); y += Math.floor((h-mh)/2); w=mw; h=mh;
        if (w*h > budget) continue;
        budget -= w*h;
        const bits = new Uint8Array(w*h);
        for (let j=0; j<h; j++) {
            for (let i=0; i<w; i++) bits[j*w+i] = owner(document.elementFromPoint(x+i+.5,y+j+.5))===n;
        }
        rows.push({n,x,y,w,h,bits,rect:[r.left,r.top,r.width,r.height],sig:n===field?null:signature(n)});
    }
    s.hits.set(seq,rows);
    if (s.hits.size>8) s.hits.delete(s.hits.keys().next().value);
}"""
_FIELD_CLICK = """(s, [seq,x,y]) => {
    if (!(""" + _FOCUS_MATCH + """)(s,'observe')) return false;
    const owner = (""" + _CLICK_OWNER + """), signature = (""" + _BUTTON_STATE + """);
    const n=owner(document.elementFromPoint(x,y));
    const hit=s.hits?.get(seq)?.find(h=>h.n===n);
    if (!hit || !n.isConnected || n.matches(':disabled,[aria-disabled=true]')) return false;
    if (hit.sig===null ? !(""" + _FOCUS_MATCH + """)(s,'edit') : hit.sig!==signature(n)) return false;
    const i=Math.floor(x)-hit.x, j=Math.floor(y)-hit.y;
    if (i<0 || i>=hit.w || j<0 || j>=hit.h || !hit.bits[j*hit.w+i]) return false;
    const r=n.getBoundingClientRect();
    return [r.left,r.top,r.width,r.height].every((v,k)=>v===hit.rect[k])
        && owner(document.elementFromPoint(Math.floor(x)+.5,Math.floor(y)+.5))===n;
}"""


class _Focus:
    def __init__(self, page):
        self.page = page
        self.handle = None
        self.first_sequence = None

    async def remember(self, sequence):
        await self.close()
        self.handle = await self.page.evaluate_handle(_REMEMBER_FOCUS)
        self.first_sequence = sequence

    async def matches(self, purpose="observe"):
        try:
            return await self.handle.evaluate(_FOCUS_MATCH, purpose)
        except Exception:
            # A navigation invalidates handles from the previous document.
            return False

    async def commit(self, text):
        # On a pipe error the native editor may already have applied the edit.
        # End the connection with an unknown result instead of retrying.
        return await self.handle.evaluate(_COMMIT, text)

    async def record_hits(self, sequence):
        await self.handle.evaluate(_FIELD_HITS, sequence)

    async def hits_control(self, event):
        return await self.handle.evaluate(_FIELD_CLICK, [event.sequence, event.x, event.y])

    async def close(self):
        if self.handle is not None:
            with suppress(Exception):
                await self.handle.evaluate('s => s.cleanup()')
            with suppress(Exception):
                await self.handle.dispose()
            self.handle = None


async def _dispatch(page, event):
    if isinstance(event, Click):
        await page.mouse.click(event.x, event.y)
    else:
        modifiers = []
        if event.modifiers & CONTROL:
            modifiers.append("Control")
        if event.modifiers & SHIFT:
            modifiers.append("Shift")
        key = "Key" + event.key.upper() if event.key in ("a", "z", "y") else event.key
        try:
            await page.keyboard.press("+".join([*modifiers, key]))
        except Exception:
            # A failed keydown/keyup pair must not leave Ctrl or Shift held for
            # a subsequent viewer. The interrupted keypress is never replayed.
            for pressed in [key, *reversed(modifiers)]:
                with suppress(Exception):
                    await page.keyboard.up(pressed)
            raise


class Session:
    """An application survives viewer disconnects. Only one paired viewer owns input."""

    def __init__(self, page, capture, token, *, poll_seconds=1.0):
        if not TOKEN_PATTERN.fullmatch(token):
            raise ValueError('无效的绘图会话配对码。')
        self.page = page
        self.capture = capture
        self.expected = AUTH_PREFIX + token.encode('ascii') + b'\n'
        self.busy = False
        self.sequence = 0
        self.poll_seconds = poll_seconds
        self.connections = set()

    async def next_frame(self):
        if self.sequence >= 0xffffffff:
            raise RuntimeError('绘图会话序号已用尽。请重启云端会话。')
        return await asyncio.wait_for(self.capture(self.page, self.sequence + 1), 20)

    async def send(self, writer, frame, focus):
        await focus.record_hits(frame.sequence)
        writer.write(encode_message(frame))
        await asyncio.wait_for(writer.drain(), 10)
        self.sequence = frame.sequence

    async def can_apply(self, event, current, focus):
        if not focus.first_sequence <= event.sequence <= current.sequence:
            return False
        if isinstance(event, TextCommit):
            # A preedit may span any number of paint-only revisions. Its target,
            # content and selection must still equal those visible when it began.
            return await focus.matches("edit")
        if isinstance(event, Click):
            if event.sequence == current.sequence:
                validate_click(event, current)
            if await focus.hits_control(event):
                return True
            return event.sequence == current.sequence and same_scene(current, await self.next_frame())
        # Focus traversal follows the same still-focused control. Typing and
        # editing can also survive repaints. Activation remains bound to a
        # current surface, since it may submit a form or open an action.
        if event.key == "Tab":
            return await focus.matches("key")
        if event.key not in ("Enter", "Escape") and await focus.matches("edit"):
            return True
        return (event.sequence == current.sequence and await focus.matches("key")
                and same_scene(current, await self.next_frame()) and await focus.matches("key"))

    async def serve_viewer(self, reader, writer):
        focus = _Focus(self.page)
        incoming = None
        try:
            await focus.remember(self.sequence + 1)
            current = await self.next_frame()
            await self.send(writer, current, focus)
            incoming = asyncio.create_task(async_read_message(reader, expected=INPUT_TYPES))
            while not self.page.is_closed():
                done, _ = await asyncio.wait({incoming}, timeout=self.poll_seconds)
                if done:
                    event = incoming.result()
                    if event is None:
                        return
                    applied = await self.can_apply(event, current, focus)
                    if applied and isinstance(event, TextCommit):
                        applied = await focus.commit(event.text)
                    elif applied:
                        await _dispatch(self.page, event)
                    writer.write(encode_message(InputAck(event.sequence, applied)))
                    await asyncio.wait_for(writer.drain(), 10)
                    del event
                    # Always advance the input sequence after an ACK. A first Tab
                    # can change focus without changing a single displayed pixel.
                    await focus.remember(self.sequence + 1)
                    current = await self.next_frame()
                    await self.send(writer, current, focus)
                    incoming = asyncio.create_task(async_read_message(reader, expected=INPUT_TYPES))
                else:
                    fresh = await self.next_frame()
                    focus_changed = not await focus.matches()
                    if focus_changed:
                        await focus.remember(self.sequence + 1)
                        fresh = await self.next_frame()
                    if focus_changed or not same_scene(fresh, current):
                        current = fresh
                        await self.send(writer, current, focus)
        finally:
            if incoming is not None:
                incoming.cancel()
                with suppress(asyncio.CancelledError, OSError, ValueError, EOFError):
                    await incoming
            await focus.close()

    async def accept(self, reader, writer):
        task = asyncio.current_task()
        self.connections.add(task)
        owned = False
        try:
            if len(self.connections) > 16:
                return
            given = await asyncio.wait_for(reader.readexactly(len(self.expected)), 5)
            if not hmac.compare_digest(given, self.expected) or self.busy:
                return
            self.busy = owned = True
            await self.serve_viewer(reader, writer)
        except (TimeoutError, EOFError, OSError, WireError):
            pass
        except Exception as exc:
            # Never include application content, URLs, or private tracing output in logs.
            print(f'本次云端绘图连接已结束（{type(exc).__name__}）。', file=sys.stderr)
        finally:
            if owned:
                self.busy = False
            writer.close()
            with suppress(OSError):
                await writer.wait_closed()
            self.connections.discard(task)

    async def close(self):
        tasks = tuple(self.connections)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


async def listen(args, token):
    from .chatgpt_process import open_chatgpt
    from .chromium_capture import capture_frame

    async with open_chatgpt(args.profile, executable=args.executable) as page:
        session = Session(page, capture_frame, token)
        server = await asyncio.start_server(session.accept, '127.0.0.1', args.port, limit=1024, backlog=8)
        async with server:
            print(f'云端原程序绘图端口 {server.sockets[0].getsockname()[1]}。请将该端口私下转发到本机。', flush=True)
            try:
                await server.serve_forever()
            finally:
                await session.close()


def main():
    parser = argparse.ArgumentParser(description='在云端运行原版 ChatGPT，输出本机可重绘的界面指令。')
    parser.add_argument('--port', type=int, default=17324)
    parser.add_argument('--profile', type=Path, default=Path.home() / '.local/share/cloud-linux-workspace/render/chatgpt')
    parser.add_argument('--executable', type=Path, default=Path('/usr/bin/chatgpt'),
                        help='云端 OpenAI 官方软件包中可执行程序的绝对路径')
    args = parser.parse_args()
    if not 0 <= args.port <= 65535:
        parser.error('端口必须在 0 至 65535 之间。')
    if not args.executable.is_absolute():
        parser.error('请输入云端原厂程序的绝对路径。')
    token = os.environ.pop('CLOUDWORKSPACE_RENDER_TOKEN', None)
    if token is None:
        if not sys.stderr.isatty():
            parser.error('后台运行时请通过 CLOUDWORKSPACE_RENDER_TOKEN 提供私有配对码。')
        token = secrets.token_urlsafe(32)
        print('本次私有配对码 ' + token, file=sys.stderr)
    if not TOKEN_PATTERN.fullmatch(token):
        parser.error('配对码须为 43 个 URL 安全字符。')
    try:
        asyncio.run(listen(args, token))
    except KeyboardInterrupt:
        pass
    except (OSError, ImportError, RuntimeError) as exc:
        print(f'云端绘图服务启动失败（{type(exc).__name__}）。请检查原厂客户端、图形会话和云端依赖。', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

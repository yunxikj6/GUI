"""Bind editing to the current control in the same verified original application."""

import asyncio
import os
import socket
import stat
import struct
from contextlib import asynccontextmanager, suppress

from cloud_render.chatgpt_process import APP_PAGE
from cloud_render.chromium_capture import _exclusive_page
from cloud_render.gnome_input import decode_input
from cloud_render.service import _COMMIT, _FOCUSED, _FOCUS_MATCH, _Focus

from .controls import require

FOCUS = struct.Struct('!IB')
_ALLOWED = """s => !!s.node?.isConnected && s.node.ownerDocument.hasFocus()
    && (s.node.isContentEditable || s.node.matches('textarea:read-write, input:read-write'))"""
# Keep one token while this very action produced the expected native field state.
# External edits, blur-and-return and a different target invalidate the saved handle.
_APPLY = (
    '(s, [command, text]) => { const matches = (' + _FOCUS_MATCH + ');'
    'const allowed = (' + _ALLOWED + '); const focused = (' + _FOCUSED + ');'
    'const commit = ('
    + _COMMIT
    + """);
    if (!allowed(s) || !matches(s, 'edit')) return false;
    const n=s.node, doc=n.ownerDocument, form=s.form && Number.isInteger(s.start) && Number.isInteger(s.end);
    let expected, start, end;
    if (form) {
        let left=s.start, right=s.end;
        if (command==='selectAll') {
            expected=s.value; start=0; end=s.value.length;
        } else {
            if (left===right && command!=='insertText') {
                // For a range, the expected edit does not depend on grapheme boundaries.
                // At a collapsed caret preserve the token only for a simple ASCII deletion.
                if (command==='delete' && left>0 && /^[ -~]$/.test(s.value[left-1])) left--;
                else if (command==='forwardDelete' && right<s.value.length && /^[ -~]$/.test(s.value[right])) right++;
            }
            const inserted=command==='insertText' ? text : '';
            expected=s.value.slice(0,left)+inserted+s.value.slice(right); start=end=left+inserted.length;
        }
    }
    let lost=false, edits=0, unexpected=false;
    const blur=()=>{lost=true};
    const nativeInput=e=>{
        edits++;
        const types={insertText:'insertText',delete:'deleteContentBackward',forwardDelete:'deleteContentForward'};
        if (!e.isTrusted || e.target!==n || e.inputType!==types[command]) unexpected=true;
    };
    n.addEventListener('blur',blur,true);
    n.addEventListener('input',nativeInput,true);
    try {
        const applied=command==='insertText' ? commit(s,text) : doc.execCommand(command);
        if (applied && form && !lost && !unexpected && n===focused() && allowed(s)
             && n.value===expected && n.selectionStart===start && n.selectionEnd===end
             && edits===(expected!==s.value ? 1 : 0)) {
            s.value=n.value; s.start=start; s.end=end; s.direction=n.selectionDirection; s.dirty=false;
        }
        return applied;
    } finally {
        n.removeEventListener('blur',blur,true);
        n.removeEventListener('input',nativeInput,true);
    }
}"""
)
_EDIT_COMMANDS = {b'A': 'selectAll', b'B': 'delete', b'D': 'forwardDelete', b'T': 'insertText'}


class Editor:
    def __init__(self, page, out, events):
        self.page = page
        self.out = out
        self.events = events
        self.focus = _Focus(page)
        self.lock = asyncio.Lock()
        self.revision = 0
        self.active = False

    def publish(self, active):
        require(self.revision < 200000, 'session focus event limit reached')
        self.revision += 1
        self.active = active
        self.events.write(FOCUS.pack(self.revision, active))

    async def check(self):
        allowed = (
            self.page.url == APP_PAGE
            and _exclusive_page(self.page)
            and (self.out / 'presented-scene.json').is_file()
        )
        same = (
            allowed
            and self.focus.handle is not None
            and await self.focus.matches('edit')
            and await self.focus.handle.evaluate(_ALLOWED)
        )
        if self.active and not same:
            self.publish(False)
        if allowed and not self.active:
            if self.focus.handle is None or not await self.focus.matches():
                await self.focus.remember(self.revision + 1)
            if await self.focus.matches('edit') and await self.focus.handle.evaluate(_ALLOWED):
                self.publish(True)
        return self.active

    async def sample(self):
        while not self.page.is_closed():
            async with self.lock:
                await self.check()
            await asyncio.sleep(0.12)

    async def apply(self, command):
        op, args = decode_input(command)
        if op not in _EDIT_COMMANDS:
            return False
        async with self.lock:
            if not await self.check() or args[0] != self.revision:
                return False
            action = [_EDIT_COMMANDS[op], args[1] if op == b'T' else '']
            applied = await self.focus.handle.evaluate(_APPLY, action)
            await self.check()
            return bool(applied)

    async def serve(self, reader, writer):
        try:
            peer = writer.get_extra_info('socket')
            _, uid, _ = struct.unpack('3i', peer.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
            require(uid == os.getuid(), 'unexpected editor connection')
            async with asyncio.timeout(3):
                length = int.from_bytes(await reader.readexactly(2), 'big')
                require(0 < length <= 4103, 'invalid bounded editor command')
                command = await reader.readexactly(length)
                require(await reader.read(1) == b'', 'extra editor bytes')
                writer.write(bytes([await self.apply(command)]))
                await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()


@asynccontextmanager
async def active_editor(page, out, pair):
    path = out / 'editor.sock'
    descriptor = os.open(pair / 'focus.events', os.O_WRONLY | os.O_APPEND | os.O_NOFOLLOW)
    with os.fdopen(descriptor, 'wb', buffering=0) as events:
        status = os.fstat(descriptor)
        require(
            stat.S_ISREG(status.st_mode)
            and status.st_uid == os.geteuid()
            and status.st_mode & 0o777 == 0o600
            and status.st_size == 0,
            'a fresh private focus channel is required',
        )
        editor = Editor(page, out, events)
        server = await asyncio.start_unix_server(editor.serve, path=path)
        try:
            os.chmod(path, 0o600)
            async with server:
                monitor = asyncio.create_task(editor.sample())
                try:
                    yield
                finally:
                    monitor.cancel()
                    with suppress(asyncio.CancelledError):
                        await monitor
                    await editor.focus.close()
                    if editor.active:
                        editor.publish(False)
        finally:
            path.unlink(missing_ok=True)


def deliver(out, command):
    path = out / 'editor.sock'
    if not path.is_socket():
        return False
    with socket.socket(socket.AF_UNIX) as client:
        client.settimeout(4)
        client.connect(str(path))
        client.sendall(len(command).to_bytes(2, 'big') + command)
        client.shutdown(socket.SHUT_WR)
        reply = client.recv(2)
        require(reply in (b'\x00', b'\x01'), 'original editor closed without acknowledging the input')
        return reply == b'\x01'

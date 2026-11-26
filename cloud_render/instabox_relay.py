"""Private, ephemeral CLRP transport for InstaBox HTTPS exec and file reads.

Each exec invokes this same stdlib-only source with one bounded JSON request on
stdin. The detached process owns one paired loopback connection. Its Unix socket
and pending message files are private to the session. No app or shell commands,
cloud credentials, target hosts, or file paths come from the control requests.
"""
import asyncio
import base64
from collections import deque
from contextlib import suppress
import hashlib
import hmac
import json
import math
import os
from pathlib import Path
import re
import signal
import socket
import stat
import struct
import sys
import time


HEADER = struct.Struct('!4sBBHI')
MAX_PAYLOAD = 32 * 1024 * 1024
INLINE = 64 * 1024
CONTROL_LIMIT = 2048
REPLY_LIMIT = 96 * 1024
IDLE = 180
SOCKET = 'control.sock'
OWNER = b'CLRP HTTPS relay 1\n'


class RelayError(Exception):
    pass


def require(condition, code):
    if not condition:
        raise RelayError(code)


def check_request(req):
    require(isinstance(req, dict) and isinstance(req.get('op'), str), 'invalid-request')
    op = req['op']
    extras = {'start': {'port', 'pair'}, 'poll': {'after'}, 'click': {'id', 'data'}, 'close': set()}
    require(op in extras and set(req) == {'op', 'session'} | extras[op], 'invalid-request')
    require(isinstance(req['session'], str) and re.fullmatch('[0-9a-f]{32}', req['session']), 'invalid-session')
    if op == 'start':
        require(type(req['port']) is int and 1 <= req['port'] <= 65535, 'invalid-port')
        require(isinstance(req['pair'], str) and re.fullmatch('[A-Za-z0-9_-]{43}', req['pair']), 'invalid-pair')
    if op == 'poll':
        require(type(req['after']) is int and 0 <= req['after'] < 2 ** 53, 'invalid-cursor')
    if op == 'click':
        require(type(req['id']) is int and 1 <= req['id'] < 2 ** 53, 'invalid-click-id')
        require(isinstance(req['data'], str) and len(req['data']) == 32, 'invalid-click')
        raw = base64.b64decode(req['data'], validate=True)
        require(len(raw) == 24 and raw[:12] == HEADER.pack(b'CLRP', 1, 2, 0, 12), 'invalid-click')
        seq, x, y = struct.unpack('!Iff', raw[12:])
        require(seq and math.isfinite(x) and math.isfinite(y) and 0 <= x < 8192 and 0 <= y < 8192, 'invalid-click')


def session_dir(session):
    return Path('/tmp') / ('clrp-' + session)


def assert_owner(root):
    info = root.lstat()
    require(stat.S_ISDIR(info.st_mode) and info.st_uid == os.geteuid() and
            stat.S_IMODE(info.st_mode) == 0o700, 'session-owner')
    marker = root / 'owner'
    metadata = marker.lstat()
    require(stat.S_ISREG(metadata.st_mode) and metadata.st_uid == info.st_uid and metadata.st_size == len(OWNER)
            and marker.read_bytes() == OWNER, 'session-owner')


def clean(root):
    """Unlink only this program's entries, without following paths or subdirectories."""
    assert_owner(root)
    for entry in root.iterdir():
        if entry.name == SOCKET or re.fullmatch(r'msg-[1-9][0-9]{0,15}\.bin(?:\.part)?', entry.name):
            entry.unlink(missing_ok=True)
    if set(p.name for p in root.iterdir()) == {'owner'}:
        (root / 'owner').unlink()
        root.rmdir()


def message_file(root, index):
    return root / f'msg-{index}.bin'


class Relay:
    def __init__(self, root, start):
        self.root = root
        self.start = start
        self.writer = None
        self.messages = deque()
        self.queued_bytes = 0
        self.acked = self.published = self.offered = 0
        self.input_id = 0
        self.input_bytes = None
        self.input_flushed = False
        self.input_lock = asyncio.Lock()
        self.changed = asyncio.Condition()
        self.ready = asyncio.Event()
        self.stopping = asyncio.Event()
        self.closed = False
        self.last_request = time.monotonic()
        self.controls = set()

    async def receive(self):
        try:
            reader, self.writer = await asyncio.wait_for(asyncio.open_connection('127.0.0.1', self.start['port']), 5)
            self.writer.write(('CLRP-AUTH/1 ' + self.start['pair'] + '\n').encode('ascii'))
            await self.writer.drain()
            while True:
                header = await reader.readexactly(HEADER.size)
                magic, version, kind, flags, size = HEADER.unpack(header)
                require(magic == b'CLRP' and version == 1 and flags == 0 and
                        ((kind == 1 and 10 <= size <= MAX_PAYLOAD) or (kind == 3 and size == 5)), 'upstream-protocol')
                require(self.published or kind == 1, 'upstream-protocol')
                total = HEADER.size + size
                async with self.changed:
                    await self.changed.wait_for(lambda: len(self.messages) < 8 and
                                                self.queued_bytes + total <= 2 * (MAX_PAYLOAD + HEADER.size))
                index = self.published + 1
                receipt = {'kind': 'message', 'id': index, 'size': total}
                if total <= INLINE:
                    payload = await asyncio.wait_for(reader.readexactly(size), 30)
                    raw = header + payload
                    if kind == 3:
                        seq, applied = struct.unpack('!IB', payload)
                        require(seq != 0 and applied <= 1, 'upstream-protocol')
                    receipt.update(data=base64.b64encode(raw).decode('ascii'), sha256=hashlib.sha256(raw).hexdigest())
                else:
                    target = message_file(self.root, index)
                    part = target.with_suffix('.bin.part')
                    digest = hashlib.sha256(header)
                    with part.open('xb') as file:
                        file.write(header)
                        remain = size
                        while remain:
                            chunk = await asyncio.wait_for(reader.readexactly(min(remain, 65536)), 30)
                            file.write(chunk)
                            digest.update(chunk)
                            remain -= len(chunk)
                    part.replace(target)
                    receipt.update(file=True, sha256=digest.hexdigest())
                async with self.changed:
                    self.messages.append(receipt)
                    self.queued_bytes += total
                    self.published = index
                    self.ready.set()
                    self.changed.notify_all()
        except (OSError, EOFError, RelayError, asyncio.TimeoutError):
            pass
        finally:
            self.closed = True
            self.ready.set()
            if self.writer:
                self.writer.close()
            async with self.changed:
                self.changed.notify_all()

    async def poll(self, after):
        async with self.changed:
            require(self.acked <= after <= self.offered, 'invalid-cursor')
            if after > self.acked:
                head = self.messages[0]
                require(head['id'] == after, 'invalid-cursor')
                if head.get('file'):
                    message_file(self.root, after).unlink()
                self.messages.popleft()
                self.queued_bytes -= head['size']
                self.acked = after
                self.changed.notify_all()
            if not self.messages and not self.closed:
                with suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(self.changed.wait_for(lambda: self.messages or self.closed), 2.5)
            # A concurrent caller can retire this cursor while the poll is asleep.
            require(after == self.acked, 'invalid-cursor')
            if self.messages:
                self.offered = self.messages[0]['id']
                return self.messages[0]
            return {'kind': 'closed' if self.closed else 'wait'}

    async def click(self, req):
        async with self.input_lock:
            index, data = req['id'], req['data']
            if index == self.input_id:
                require(hmac.compare_digest(data, self.input_bytes), 'click-conflict')
                require(self.input_flushed, 'unconfirmed-click')
                return {'id': index}
            require(index == self.input_id + 1, 'invalid-click-id')
            require(self.published and not self.closed, 'upstream-closed')
            # Mark consumed before writing to the transport. An interrupted write
            # has an unknown application outcome and must never be attempted twice.
            self.input_id, self.input_bytes, self.input_flushed = index, data, False
            try:
                self.writer.write(base64.b64decode(data, validate=True))
                await asyncio.wait_for(self.writer.drain(), 5)
                self.input_flushed = True
            except (OSError, asyncio.TimeoutError):
                self.writer.close()
                raise RelayError('unconfirmed-click') from None
            return {'id': index}

    async def control(self, reader, writer):
        task = asyncio.current_task()
        self.controls.add(task)
        close = False
        try:
            require(len(self.controls) <= 16, 'busy')
            line = await asyncio.wait_for(reader.readline(), 3)
            require(len(line) <= CONTROL_LIMIT, 'invalid-request')
            req = json.loads(line)
            check_request(req)
            require(hmac.compare_digest(self.start['session'], req['session']), 'invalid-session')
            self.last_request = time.monotonic()
            op = req['op']
            result = {}
            if op == 'start':
                require(self.start['port'] == req['port'] and hmac.compare_digest(self.start['pair'], req['pair']),
                        'session-conflict')
                await asyncio.wait_for(self.ready.wait(), 35)
                require(self.published, 'upstream-unavailable')
            elif op == 'poll':
                result = await self.poll(req['after'])
            elif op == 'click':
                result = await self.click(req)
            else:
                close = True
            reply = {'ok': True, **result}
        except RelayError as exc:
            reply = {'ok': False, 'error': str(exc)}
        except (EOFError, OSError, ValueError, asyncio.TimeoutError):
            reply = {'ok': False, 'error': 'control-failed'}
        try:
            writer.write(json.dumps(reply, separators=(',', ':')).encode() + b'\n')
            await asyncio.wait_for(writer.drain(), 3)
        except (OSError, asyncio.TimeoutError):
            pass
        finally:
            writer.close()
            self.controls.discard(task)
            if close:
                self.stopping.set()

    async def run(self, listener):
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGHUP, signal.SIGTERM):
            loop.add_signal_handler(sig, self.stopping.set)
        source = asyncio.create_task(self.receive())
        try:
            async with await asyncio.start_unix_server(self.control, sock=listener, limit=CONTROL_LIMIT):
                while not self.stopping.is_set():
                    remaining = IDLE - (time.monotonic() - self.last_request)
                    if remaining <= 0:
                        break
                    with suppress(asyncio.TimeoutError):
                        await asyncio.wait_for(self.stopping.wait(), min(remaining, 10))
        finally:
            source.cancel()
            for task in self.controls.copy():
                task.cancel()
            await asyncio.gather(source, *self.controls, return_exceptions=True)


def launch(root, req):
    os.umask(0o077)
    root.mkdir(mode=0o700)
    try:
        (root / 'owner').write_bytes(OWNER)
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        with listener:
            listener.bind(str(root / SOCKET))
            listener.listen(16)
            listener.setblocking(False)
            if os.fork() == 0:
                try:
                    os.setsid()
                    keep = listener.fileno()
                    null = os.open(os.devnull, os.O_RDWR)
                    for std in (0, 1, 2):
                        os.dup2(null, std)
                    for fd in os.listdir('/proc/self/fd'):
                        n = int(fd)
                        if n > 2 and n != keep:
                            with suppress(OSError):
                                os.close(n)
                    asyncio.run(Relay(root, req).run(listener))
                finally:
                    with suppress(OSError, RelayError):
                        clean(root)
                    os._exit(0)
    except Exception:
        with suppress(OSError, RelayError):
            clean(root)
        raise


def execute(req):
    check_request(req)
    root = session_dir(req['session'])
    if req['op'] == 'start':
        try:
            launch(root, req)
        except FileExistsError:
            pass  # A retried start must rendezvous with the same owner and upstream.
    # Concurrent starts can observe mkdir before the creator has published its
    # owner marker and listener. Only the first process creates an upstream.
    deadline = time.monotonic() + (2 if req['op'] == 'start' else 0)
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as control:
        control.settimeout(40 if req['op'] == 'start' else 8)
        while True:
            try:
                assert_owner(root)
                control.connect(str(root / SOCKET))
                break
            except (FileNotFoundError, ConnectionRefusedError) as exc:
                if time.monotonic() < deadline:
                    time.sleep(.02)
                    continue
                if req['op'] == 'close':
                    if not root.exists():
                        return {'ok': True}
                    # SIGKILL can leave a dead Unix socket and unpublished
                    # files behind. Never retire a socket still being bound.
                    if isinstance(exc, ConnectionRefusedError) and time.time() - (root / 'owner').stat().st_mtime > 5:
                        clean(root)
                        require(not root.exists(), 'cleanup-pending')
                        return {'ok': True}
                raise RelayError('session-not-found') from None
        control.sendall(json.dumps(req, separators=(',', ':')).encode() + b'\n')
        with control.makefile('rb') as stream:
            line = stream.readline(REPLY_LIMIT + 1)
        require(line.endswith(b'\n') and len(line) <= REPLY_LIMIT, 'invalid-reply')
        reply = json.loads(line)
    if req['op'] == 'close' and reply['ok']:
        deadline = time.monotonic() + 3
        while root.exists() and time.monotonic() < deadline:
            time.sleep(.02)
        require(not root.exists(), 'cleanup-pending')
    return reply


def main():
    try:
        body = sys.stdin.buffer.read(CONTROL_LIMIT + 1)
        require(len(body) <= CONTROL_LIMIT, 'invalid-request')
        result = execute(json.loads(body))
    except RelayError as exc:
        result = {'ok': False, 'error': str(exc)}
    except (TypeError, ValueError, OSError, TimeoutError):
        result = {'ok': False, 'error': 'control-failed'}
    print(json.dumps(result, separators=(',', ':')))


if __name__ == '__main__':
    main()
